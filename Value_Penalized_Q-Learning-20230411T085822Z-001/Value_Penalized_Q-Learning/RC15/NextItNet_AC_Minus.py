import os
import time
import trfl
import argparse
import numpy as np
import pandas as pd
from utility import *
import tensorflow as tf
from NextItNetModules import *
from trfl import indexing_ops
from joblib import Parallel,delayed

def parse_args():
    parser = argparse.ArgumentParser(description="Next-AC-Minus.")
    parser.add_argument('--epoch', type=int, default=30,
                        help='Number of max epochs.')
    parser.add_argument('--data', nargs='?', default='data',
                        help='data directory')
    parser.add_argument('--batch_size', type=int, default=256,
                        help='Batch size.')
    parser.add_argument('--hidden_factor', type=int, default=64,
                        help='Number of hidden factors, i.e., embedding size.')
    parser.add_argument('--r_click', type=float, default=0.2,
                        help='reward for the click behavior.')
    parser.add_argument('--r_buy', type=float, default=1.0,
                        help='reward for the purchase behavior.')
    parser.add_argument('--lr', type=float, default=0.005,
                        help='Learning rate.') # 0.005 for batchsize  = 256
    parser.add_argument('--discount', type=float, default=0.5,
                        help='Discount factor for RL.')
    parser.add_argument('--out', type=str, help='log file name')
    parser.add_argument('--gpu', type=str, help='gpu id')        
    parser.add_argument('--method', type=str, default='unspecified')
    parser.add_argument('--coef', type=float, default=0)
    parser.add_argument('--num_multi_head', type=int, default=15)
    return parser.parse_args()


class NextItNet:
    def __init__(self, hidden_size, learning_rate, item_num, state_size, coef, num_multi_head,
                name='NextRec', method='unspecified'):
        self.state_size = state_size
        self.learning_rate = learning_rate
        self.hidden_size=hidden_size
        self.item_num=int(item_num)
        self.flag = tf.constant(value=True, dtype=tf.bool)
        self.name = name
        self.num_multi_head = num_multi_head
        with tf.variable_scope(self.name):

            self.all_embeddings=self.initialize_embeddings()

            self.inputs = tf.placeholder(tf.int32, [None, state_size],name='inputs')
            self.len_state=tf.placeholder(tf.int32, [None],name='len_state')
            self.target= tf.placeholder(tf.int32, [None],name='target')
            self.rco = tf.placeholder(tf.float32, shape=(num_multi_head,), name='random_coef')
            self.is_training = tf.placeholder(tf.bool, shape=(), name='is_traing')
            self.add_penalty = tf.placeholder(tf.bool, shape=(), name='whether_add_penalty')
            mask = tf.expand_dims(tf.to_float(tf.not_equal(self.inputs, item_num)), -1)

            # self.input_emb=tf.nn.embedding_lookup(all_embeddings['state_embeddings'],self.inputs)
            self.model_para = {
                'dilated_channels': 64,  # larger is better until 512 or 1024
                'dilations': [1, 2, 1, 2, 1, 2, ],  # YOU should tune this hyper-parameter, refer to the paper.
                'kernel_size': 3,
            }

            context_embedding = tf.nn.embedding_lookup(self.all_embeddings['state_embeddings'],
                                                       self.inputs)
            context_embedding *= mask

            dilate_output = context_embedding
            for layer_id, dilation in enumerate(self.model_para['dilations']):
                dilate_output = nextitnet_residual_block(dilate_output, dilation,
                                                        layer_id, self.model_para['dilated_channels'],
                                                        self.model_para['kernel_size'], 
                                                        causal=True, train=self.is_training)
                dilate_output *= mask
            # state_hidden 64-D
            self.state_hidden = extract_axis_1(dilate_output, self.len_state - 1)
            
            multi_head_output = tf.contrib.layers.fully_connected(self.state_hidden, 
                                    self.item_num * self.num_multi_head, 
                                    activation_fn=None, scope='multi-head')
            multi_head_output = tf.reshape(multi_head_output, (tf.shape(multi_head_output)[0], 
                                                                self.item_num, self.num_multi_head))
            out_rem = multi_head_output * tf.reshape(self.rco, [1,1,-1])
            
            def _add_penalty_true_fn_rem():
                if coef != 0:
                    std = tf.math.reduce_std(multi_head_output, axis=-1)
                    return tf.math.reduce_sum(out_rem, axis=-1) - coef * tf.stop_gradient(std) 
                else:
                    return tf.math.reduce_sum(out_rem, axis=-1)
            def _add_penalty_false_fn_rem():
                return tf.math.reduce_sum(out_rem, axis=-1) 

            if method == 'baseline':
                self.output1 = tf.contrib.layers.fully_connected(self.state_hidden, self.item_num,
                                                             activation_fn=None, scope="q-value")
            elif method == 'mean':
                self.output1 = tf.math.reduce_mean(multi_head_output, axis=-1)
            elif method =='rem':
                self.output1 = tf.cond(pred=tf.equal(self.add_penalty, self.flag), 
                                        true_fn=_add_penalty_true_fn_rem,
                                        false_fn=_add_penalty_false_fn_rem)
                                        
            self.output2 = tf.contrib.layers.fully_connected(self.state_hidden, self.item_num,
                                                             activation_fn=None, scope="ce-logits")  # all ce logits

            # TRFL way
            self.actions = tf.placeholder(tf.int32, [None])
            self.targetQs_ = tf.placeholder(tf.float32, [None, item_num])
            self.targetQs_selector = tf.placeholder(tf.float32, [None,
                                                                 item_num])  # used for select best action for double q learning
            self.reward = tf.placeholder(tf.float32, [None])
            self.discount = tf.placeholder(tf.float32, [None])

            
            qloss, q_learning = trfl.double_qlearning(self.output1, self.actions, self.reward, 
                                                    self.discount, self.targetQs_, self.targetQs_selector)
            q_indexed = tf.stop_gradient(indexing_ops.batched_index(self.output1, self.actions))
            naive_celoss = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=self.actions, 
                                                                            logits=self.output2)
            celoss = tf.multiply(q_indexed, naive_celoss)
            self.q_mean = tf.math.reduce_mean(q_indexed)
            self.q_std  = tf.math.reduce_std(q_indexed)

            self.loss = tf.reduce_mean(qloss + celoss)
            self.q_loss = tf.reduce_mean(qloss)
            self.ce_loss = tf.reduce_mean(celoss)
            self.naive_celoss = tf.reduce_mean(naive_celoss)
            self.opt = tf.train.AdamOptimizer(learning_rate).minimize(self.loss)

    def initialize_embeddings(self):
        all_embeddings = dict()
        state_embeddings= tf.Variable(tf.random_normal([self.item_num+1, self.hidden_size], 0.0, 0.01),
            name='state_embeddings')
        all_embeddings['state_embeddings']=state_embeddings
        return all_embeddings

def evaluate(sess):
    eval_sessions=pd.read_pickle(os.path.join(data_directory, 'sampled_test.df'))
    eval_ids = eval_sessions.session_id.unique()
    groups = eval_sessions.groupby('session_id')
    batch = 100
    evaluated=0
    total_clicks=0.0
    total_purchase = 0.0
    total_reward = [0, 0, 0, 0]
    hit_clicks=[0,0,0,0]
    ndcg_clicks=[0,0,0,0]
    hit_purchase=[0,0,0,0]
    ndcg_purchase=[0,0,0,0]
    while evaluated<len(eval_ids):
        states, len_states, actions, rewards = [], [], [], []
        for i in range(batch):
            if evaluated==len(eval_ids):
                break
            id=eval_ids[evaluated]
            group=groups.get_group(id)
            history=[]
            for index, row in group.iterrows():
                state=list(history)
                len_states.append(state_size 
                        if len(state)>=state_size else 1 if len(state)==0 else len(state))
                state=pad_history(state,state_size,item_num)
                states.append(state)
                action=row['item_id']
                is_buy=row['is_buy']
                reward = reward_buy if is_buy == 1 else reward_click
                if is_buy==1:
                    total_purchase+=1.0
                else:
                    total_clicks+=1.0
                actions.append(action)
                rewards.append(reward)
                history.append(row['item_id'])
            evaluated+=1

        prediction=sess.run(NextRec1.output2, 
            feed_dict={NextRec1.inputs: states,NextRec1.len_state:len_states,NextRec1.is_training:False})

        n_jobs = len(prediction) // 36 + 1
        if n_jobs > 12:
            n_jobs = 12

        res = Parallel(n_jobs=n_jobs, prefer='threads')(delayed(np.argsort)(part) for part in np.array_split(prediction, n_jobs))
        FirstIter = True
        sorted_list = None
        for part in res:
            if FirstIter:
                sorted_list = np.copy(part)
                FirstIter = False
            else:
                sorted_list = np.concatenate([sorted_list, part])
        calculate_hit(sorted_list,topk,actions,rewards,reward_click,total_reward,
                        hit_clicks,ndcg_clicks,hit_purchase,ndcg_purchase)
    best_rec = [[0,0,0,0,0], [0,0,0,0,0], [0,0,0,0,0], [0,0,0,0,0]]
    for i in range(len(topk)):
        hr_click=hit_clicks[i]/total_clicks
        hr_purchase=hit_purchase[i]/total_purchase
        ng_click=ndcg_clicks[i]/total_clicks
        ng_purchase=ndcg_purchase[i]/total_purchase
        print('\t\t'* (topk[i] // 5) + 'reward  @%d : %f' % (topk[i],total_reward[i]))
        print('\t\t'* (topk[i] // 5) + 'c hr ng @%d : %f, %f' % (topk[i],hr_click,ng_click))
        print('\t\t'* (topk[i] // 5) + 'p hr ng @%d : %f, %f' % (topk[i], hr_purchase, ng_purchase))

        best_rec[i][0] = total_reward[i]
        best_rec[i][1] = hr_click
        best_rec[i][2] = float(ng_click)
        best_rec[i][3] = hr_purchase
        best_rec[i][4] = float(ng_purchase)

    return np.array(best_rec).reshape(1, -1)[0]

if __name__ == '__main__':
    start_time = time.time()
    # Network parameters
    args = parse_args()
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu

    data_directory = args.data
    data_statis = pd.read_pickle(
        os.path.join(data_directory, 'data_statis.df'))  # read data statistics, includeing state_size and item_num
    state_size = data_statis['state_size'][0]  # the length of history to define the state
    item_num = data_statis['item_num'][0]  # total number of items
    reward_click = args.r_click
    reward_buy = args.r_buy
    topk=[5,10,15,20]
    # save_file = 'pretrain-GRU/%d' % (hidden_size)

    tf.reset_default_graph()
    NextRec1 = NextItNet(hidden_size=args.hidden_factor, learning_rate=args.lr, 
                        item_num=item_num, state_size=state_size, coef=args.coef,
                        num_multi_head=args.num_multi_head,
                        name='NextRec1', method=args.method)
    NextRec2 = NextItNet(hidden_size=args.hidden_factor, learning_rate=args.lr, 
                        item_num=item_num,state_size=state_size, coef=args.coef,
                        num_multi_head=args.num_multi_head,
                        name='NextRec2', method=args.method)

    replay_buffer = pd.read_pickle(os.path.join(data_directory, 'replay_buffer.df'))
    total_step=0

    log_data = []
    total_score_rec = []
    q_mean_log = []
    q_std_log = []
    column_name = ['rew@5', 'hr_c@5', 'ng_c@5', 'hr_p@5', 'ng_p@5', 
					'rew@10', 'hr_c@10', 'ng_c@10', 'hr_p@10', 'ng_p@10', 
					'rew@15', 'hr_c@15', 'ng_c@15', 'hr_p@15', 'ng_p@15',
					'rew@20', 'hr_c@20', 'ng_c@20', 'hr_p@20', 'ng_p@20']

    gpu_options = tf.GPUOptions(allow_growth=True)
    with tf.Session(config=tf.ConfigProto(gpu_options=gpu_options)) as sess:
        # Initialize variables
        sess.run(tf.global_variables_initializer())
        # evaluate(sess)
        num_rows=replay_buffer.shape[0]
        num_batches=int(num_rows/args.batch_size)
        print('epoch = {}    num_batches = {}'.format(args.epoch, num_batches))
        for i in range(args.epoch):
            for j in range(num_batches):
                # batch = entropy_correct_replay(replay_buffer)
                batch = replay_buffer.sample(n=args.batch_size).to_dict()
                next_state = list(batch['next_state'].values())
                len_next_state = list(batch['len_next_states'].values())
                # double q learning, pointer is for selecting which network  is target and which is main
                pointer = np.random.randint(0, 2)
                if pointer == 0:
                    mainQN = NextRec1
                    target_QN = NextRec2
                else:
                    mainQN = NextRec2
                    target_QN = NextRec1
                # 无论选取的主网络选取那个NextRecNet，target_Qs target_Qs_selector 的输出都是 1 x 40783 
                random_coef = make_coeff(args.num_multi_head)
                unifor_coef = [1/args.num_multi_head for _ in range(args.num_multi_head)]
                target_Qs = sess.run(target_QN.output1,
                                     feed_dict={target_QN.inputs: next_state,
                                                target_QN.len_state: len_next_state,
                                                target_QN.rco: random_coef,
                                                target_QN.is_training: True,
                                                target_QN.add_penalty: True})
                target_Qs_selector = sess.run(mainQN.output1,
                                              feed_dict={mainQN.inputs: next_state,
                                                         mainQN.len_state: len_next_state,
                                                         mainQN.rco: unifor_coef,
                                                         mainQN.is_training: True,
                                                         mainQN.add_penalty: True})
                # Set target_Qs to 0 for states where episode ends
                is_done = list(batch['is_done'].values())
                for index in range(target_Qs.shape[0]):
                    if is_done[index]:
                        target_Qs[index] = np.zeros([item_num])

                state = list(batch['state'].values())
                len_state = list(batch['len_state'].values())
                action = list(batch['action'].values())
                is_buy = list(batch['is_buy'].values())
                reward = []
                for k in range(len(is_buy)):
                    reward.append(reward_buy if is_buy[k] == 1 else reward_click)
                discount = [args.discount] * len(action)
                loss, _, qloss, celoss, naive_celoss, q_mean, q_std = sess.run([
                                            mainQN.loss, mainQN.opt, mainQN.q_loss, 
                                            mainQN.ce_loss, mainQN.naive_celoss,
                                            mainQN.q_mean, mainQN.q_std],
                                   feed_dict={mainQN.inputs: state,
                                              mainQN.len_state: len_state,
                                              mainQN.targetQs_: target_Qs,
                                              mainQN.reward: reward,
                                              mainQN.discount: discount,
                                              mainQN.actions: action,
                                              mainQN.targetQs_selector: target_Qs_selector,
                                              mainQN.rco: random_coef,
                                              mainQN.is_training: True,
                                              mainQN.add_penalty: False})
                total_step += 1
                if total_step % 100 == 0:
                    print("the naive_celoss is %.3f  weighted celoss is: %.3f  qloss is %.3f" % 
                                                                        (naive_celoss, celoss, qloss))
                    print('q_mean and q_std is\t\t\t\t  %.3f     %.3f' % (q_mean, q_std))
                    q_mean_log.append(q_mean)
                    q_std_log.append(q_std)
                if total_step % 2000 == 0:
                    if args.method != 'baseline' and total_step < 2000 * 0:
                        pass
                    else:
                        print('\nstart to eval')
                        time_eval_start = time.time()
                        log_data_one_eval = evaluate(sess)
                        print('time used in one eval', time.time() - time_eval_start)
                        total_score = log_data_one_eval[log_data_one_eval<1].sum()
                        print('total socre ',total_score )
                        total_score_rec.append(np.round(total_score, 3))
                        print('total score record', total_score_rec)
                        log_data.append(log_data_one_eval)
    print('time used in NextItNet_AC :', time.time() - start_time)
    log_data = pd.DataFrame(log_data, columns=column_name)
    log_data.to_csv('log_data/' + args.out + '.csv')
    pd.DataFrame(q_mean_log).to_csv('log_data_next_search/q_mean_log_' + args.out + '.csv')
    pd.DataFrame(q_std_log).to_csv('log_data_next_search/q_std_log_' + args.out + '.csv')