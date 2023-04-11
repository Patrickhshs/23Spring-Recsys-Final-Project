import tensorflow as tf
import numpy as np
import pandas as pd
import os
import argparse
import trfl
from utility import *
from SASRecModules import *
import time
from joblib import Parallel,delayed


def parse_args():
    parser = argparse.ArgumentParser(description="Run supervised GRU.")

    parser.add_argument('--epoch', type=int, default=10,
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
                        help='Learning rate.')
    parser.add_argument('--num_heads', default=1, type=int)
    parser.add_argument('--num_blocks', default=1, type=int)
    parser.add_argument('--dropout_rate', default=0.1, type=float)
    parser.add_argument('--out', type=str, help='log file name')
    parser.add_argument('--gpu', type=str, help='gpu id', default=0)
    return parser.parse_args()


class SASRecnetwork:
    def __init__(self, hidden_size,learning_rate,item_num,state_size):
        self.state_size = state_size
        self.learning_rate = learning_rate
        self.hidden_size=hidden_size
        self.item_num=int(item_num)
        self.is_training = tf.placeholder(tf.bool, shape=())

        all_embeddings=self.initialize_embeddings()

        self.inputs = tf.placeholder(tf.int32, [None, state_size],name='inputs')
        self.len_state=tf.placeholder(tf.int32, [None],name='len_state')
        self.target= tf.placeholder(tf.int32, [None],name='target') # target item, to calculate ce loss

        self.input_emb=tf.nn.embedding_lookup(all_embeddings['state_embeddings'],self.inputs)
        # Positional Encoding
        pos_emb=tf.nn.embedding_lookup(all_embeddings['pos_embeddings'],tf.tile(tf.expand_dims(tf.range(tf.shape(self.inputs)[1]), 0), [tf.shape(self.inputs)[0], 1]))
        self.seq=self.input_emb+pos_emb

        mask = tf.expand_dims(tf.to_float(tf.not_equal(self.inputs, item_num)), -1)
        #Dropout
        self.seq = tf.layers.dropout(self.seq,
                                     rate=args.dropout_rate,
                                     training=tf.convert_to_tensor(self.is_training))
        self.seq *= mask

        # Build blocks

        for i in range(args.num_blocks):
            with tf.variable_scope("num_blocks_%d" % i):
                # Self-attention
                self.seq = multihead_attention(queries=normalize(self.seq),
                                               keys=self.seq,
                                               num_units=self.hidden_size,
                                               num_heads=args.num_heads,
                                               dropout_rate=args.dropout_rate,
                                               is_training=self.is_training,
                                               causality=True,
                                               scope="self_attention")

                # Feed forward
                self.seq = feedforward(normalize(self.seq), num_units=[self.hidden_size, self.hidden_size],
                                       dropout_rate=args.dropout_rate,
                                       is_training=self.is_training)
                self.seq *= mask

        self.seq = normalize(self.seq)
        self.state_hidden=extract_axis_1(self.seq, self.len_state - 1)

        self.output = tf.contrib.layers.fully_connected(self.state_hidden,self.item_num,activation_fn=None,scope='fc')

        self.loss=tf.nn.sparse_softmax_cross_entropy_with_logits(labels=self.target,logits=self.output)
        self.loss = tf.reduce_mean(self.loss)
        self.opt = tf.train.AdamOptimizer(self.learning_rate).minimize(self.loss)


    def initialize_embeddings(self):
        all_embeddings = dict()
        state_embeddings= tf.Variable(tf.random_normal([self.item_num+1, self.hidden_size], 0.0, 0.01),
            name='state_embeddings')
        pos_embeddings=tf.Variable(tf.random_normal([self.state_size, self.hidden_size], 0.0, 0.01),
            name='pos_embeddings')
        all_embeddings['state_embeddings']=state_embeddings
        all_embeddings['pos_embeddings']=pos_embeddings
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
                len_states.append(state_size if len(state)>=state_size else 1 if len(state)==0 else len(state))
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
        prediction=sess.run(SASRec.output, feed_dict={SASRec.inputs: states,SASRec.len_state:len_states,SASRec.is_training:False})
        n_jobs = 4

        res = Parallel(n_jobs=n_jobs, prefer='threads')(delayed(np.argsort)(part) for part in np.array_split(prediction, n_jobs))
        FirstIter = True
        sorted_list = None
        for part in res:
            if FirstIter:
                sorted_list = np.copy(part)
                FirstIter = False
            else:
                sorted_list = np.concatenate([sorted_list, part])
        calculate_hit(sorted_list,topk,actions,rewards,reward_click,total_reward,hit_clicks,ndcg_clicks,hit_purchase,ndcg_purchase)
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
    # Network parameters
    args = parse_args()
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu
    start_time = time.time()

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

    SASRec = SASRecnetwork(hidden_size=args.hidden_factor, learning_rate=args.lr,item_num=item_num,state_size=state_size)

    replay_buffer = pd.read_pickle(os.path.join(data_directory, 'replay_buffer.df'))
    # saver = tf.train.Saver()

    total_step=0
    log_data = []
    total_score_rec = []
    column_name = ['rew@5', 'hr_c@5', 'ng_c@5', 'hr_p@5', 'ng_p@5', 
					'rew@10', 'hr_c@10', 'ng_c@10', 'hr_p@10', 'ng_p@10', 
					'rew@15', 'hr_c@15', 'ng_c@15', 'hr_p@15', 'ng_p@15',
					'rew@20', 'hr_c@20', 'ng_c@20', 'hr_p@20', 'ng_p@20']
    with tf.Session() as sess:
        # Initialize variables
        sess.run(tf.global_variables_initializer())
        # evaluate(sess)
        num_rows=replay_buffer.shape[0]
        num_batches=int(num_rows/args.batch_size)
        for i in range(args.epoch):
            for j in range(num_batches):
                batch = replay_buffer.sample(n=args.batch_size).to_dict()
                state = list(batch['state'].values())
                len_state = list(batch['len_state'].values())
                target=list(batch['action'].values())
                # seq,seq_test=sess.run([SASRec.seq,SASRec.seq_test],
                #                    feed_dict={SASRec.inputs: state,
                #                               SASRec.len_state: len_state,
                #                               SASRec.target: target,
                #                               SASRec.is_training:True})
                loss, _ = sess.run([SASRec.loss, SASRec.opt],
                                   feed_dict={SASRec.inputs: state,
                                              SASRec.len_state: len_state,
                                              SASRec.target: target,
                                              SASRec.is_training:True})
                total_step+=1
                if total_step % 200 == 0:
                    print("the loss in %dth batch is: %f" % (total_step, loss))
                if total_step % 2000 == 0:
                    print('\nstart to eval')
                    time_eval_start = time.time()
                    log_data_one_eval = evaluate(sess)
                    print('time used in one eval', time.time() - time_eval_start)
                    total_score = log_data_one_eval[log_data_one_eval<1].sum()
                    print('total socre ',total_score )
                    total_score_rec.append(np.round(total_score, 3))
                    print('total score record', total_score_rec)
                    log_data.append(log_data_one_eval)
    print('time used in SAS :', time.time() - start_time)
    log_data = pd.DataFrame(log_data, columns=column_name)
    log_data.to_csv('log_data/' + args.out + '.csv')
    print('write log done')

