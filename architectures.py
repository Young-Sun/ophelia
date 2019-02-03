from data_load import get_batch, load_vocab
from modules import *
from networks import TextEnc, AudioEnc, AudioDec, Attention, SSRN
import tensorflow as tf
from utils import *
from utils import get_global_attention_guide

class Graph(object):

    def __init__(self, hp, mode="train", reuse=None):


        assert mode in ['train', 'synthesize']
        #self.char2idx, self.idx2char = load_vocab(hp)

        self.training = True if mode=="train" else False
        self.reuse = reuse
        self.hp = hp
        self.add_data(reuse=reuse)                     ## TODO: reuse?? 

        self.build_model()

        ## TODO: only for training? :--
        #with tf.variable_scope("gs"):
        #    self.global_step = tf.Variable(0, name='global_step', trainable=False)

        if self.training:
            self.build_loss()
            self.build_training_scheme()


    def add_data(self, reuse=None):
        '''
        Add either variables (for training) or placeholders (for synthesis) to the graph
        '''
        # Data Feeding
        ## L: Text. (B, N), int32
        ## mels: Reduced melspectrogram. (B, T/r, n_mels) float32
        ## mags: Magnitude. (B, T, n_fft//2+1) float32
        hp = self.hp
        if self.training:
            ## speaker_codes will be at index 4 if present ; attention_guide will be at index -2 if present; 
            batchdict = get_batch(hp, self.get_batchsize(), get_speaker_codes=hp.multispeaker, n_utts=hp.n_utts)

            if 0: print (batchdict) ; print (batchdict.keys()) ; sys.exit('vsfbd')

            self.L, self.mels, self.mags, self.fnames, self.num_batch = \
                batchdict['text'], batchdict['mel'], batchdict['mag'], batchdict['fname'], batchdict['num_batch'] 
            #self.num_batch = batchlist[-1]

            if hp.multispeaker:
                self.speakers = batchdict['speakers'] # batchlist[4]
            else:
                self.speakers = None
            if hp.attention_guide_dir:
                #self.gts = batchlist[-2]
                self.gts = batchdict['attention_guide']
            else:
                self.gts = tf.convert_to_tensor(get_global_attention_guide(hp))



            if 0:
                print('Got batch:')
                print(self.L)
                print(self.mels)
                print(self.mags)
                print(self.fnames)
                print(self.num_batch)
                print(self.speakers)
                sys.exit('qwucfbeoivboweibv')
                # Tensor("bucket_by_sequence_length/bucket/dequeue_top:2", shape=(32, ?), dtype=int32, device=/device:CPU:0)
                # Tensor("bucket_by_sequence_length/bucket/dequeue_top:3", shape=(32, ?, 62), dtype=float32, device=/device:CPU:0)
                # Tensor("bucket_by_sequence_length/bucket/dequeue_top:4", shape=(32, ?, 1025), dtype=float32, device=/device:CPU:0)
                # Tensor("bucket_by_sequence_length/bucket/dequeue_top:5", shape=(32,), dtype=string, device=/device:CPU:0)

            batchsize = self.get_batchsize()
            self.prev_max_attentions = tf.ones(shape=(batchsize,), dtype=tf.int32)
            
        else:  # Synthesize
            self.L = tf.placeholder(tf.int32, shape=(None, None))
            self.speakers = None
            if hp.multispeaker:
                self.speakers = tf.placeholder(tf.int32, shape=(None, None)) # (B x 1)
            self.mels = tf.placeholder(tf.float32, shape=(None, None, hp.n_mels))
            self.prev_max_attentions = tf.placeholder(tf.int32, shape=(None,))

    def build_training_scheme(self):
        hp = self.hp

        self.global_step = tf.Variable(0, name='global_step', trainable=False)

        self.lr = learning_rate_decay(hp.lr, self.global_step)
        self.optimizer = tf.train.AdamOptimizer(learning_rate=self.lr)
        tf.summary.scalar("lr", self.lr)

        ## gradient clipping
        self.gvs = self.optimizer.compute_gradients(self.loss)
        self.clipped = []
        for grad, var in self.gvs:
            grad = tf.clip_by_value(grad, -1., 1.)
            self.clipped.append((grad, var))
            self.train_op = self.optimizer.apply_gradients(self.clipped, global_step=self.global_step)

        # Summary
        self.merged = tf.summary.merge_all()




class SSRNGraph(Graph):

    def get_batchsize(self):
        return self.hp.batchsize['ssrn']   ## TODO: naming?

    def build_model(self):
        with tf.variable_scope("SSRN"):
            ## OSW: use 'mels' for input both in training and synthesis -- can be either variable or placeholder 
            self.Z_logits, self.Z = SSRN(self.hp, self.mels, training=self.training, speaker_codes=self.speakers, reuse=self.reuse)

    def build_loss(self):
 
        # mag L1 loss
        self.loss_mags = tf.reduce_mean(tf.abs(self.Z - self.mags))

        # mag binary divergence loss
        self.loss_bd2 = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=self.Z_logits, labels=self.mags))

        # total loss
        self.lw_mag = self.hp.lw_mag
        self.lw_bd2 = self.hp.lw_bd2                    
        self.loss = (self.lw_mag * self.loss_mags) + (self.lw_bd2 * self.loss_bd2)

        self.loss_components = [self.loss, self.loss_mags, self.loss_bd2]

        tf.summary.scalar('train/loss_mags', self.loss_mags)
        tf.summary.scalar('train/loss_bd2', self.loss_bd2)
        tf.summary.image('train/mag_gt', tf.expand_dims(tf.transpose(self.mags[:1], [0, 2, 1]), -1))
        tf.summary.image('train/mag_hat', tf.expand_dims(tf.transpose(self.Z[:1], [0, 2, 1]), -1))






class Text2MelGraph(Graph):

    def get_batchsize(self):
        return self.hp.batchsize['t2m'] ## TODO: naming?

    def build_model(self):
        with tf.variable_scope("Text2Mel"):
            # Get S or decoder inputs. (B, T//r, n_mels)
            self.S = tf.concat((tf.zeros_like(self.mels[:, :1, :]), self.mels[:, :-1, :]), 1)

            # Networks
            with tf.variable_scope("TextEnc"):
                self.K, self.V = TextEnc(self.hp, self.L, training=self.training, speaker_codes=self.speakers, reuse=self.reuse)  # (N, Tx, e)

            with tf.variable_scope("AudioEnc"):
                self.Q = AudioEnc(self.hp, self.S, training=self.training, speaker_codes=self.speakers, reuse=self.reuse)

            with tf.variable_scope("Attention"):
                # R: (B, T/r, 2d)
                # alignments: (B, N, T/r)
                # max_attentions: (B,)
                self.R, self.alignments, self.max_attentions = Attention(self.hp, self.Q, self.K, self.V,
                                                                         mononotic_attention=(not self.training),
                                                                         prev_max_attentions=self.prev_max_attentions)
            with tf.variable_scope("AudioDec"):
                self.Y_logits, self.Y = AudioDec(self.hp, self.R, training=self.training, speaker_codes=self.speakers, reuse=self.reuse) # (B, T/r, n_mels)

    def build_loss(self):
        hp = self.hp
        # mel L1 loss
        self.loss_mels = tf.reduce_mean(tf.abs(self.Y - self.mels))

        # mel binary divergence loss
        self.loss_bd1 = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=self.Y_logits, labels=self.mels))

        # guided_attention loss
        self.A = tf.pad(self.alignments, [(0, 0), (0, hp.max_N), (0, hp.max_T)], mode="CONSTANT", constant_values=-1.)[:, :hp.max_N, :hp.max_T]
        if hp.attention_guide_dir:
            self.gts = tf.pad(self.gts, [(0, 0), (0, hp.max_N), (0, hp.max_T)], mode="CONSTANT", constant_values=1.0)[:, :hp.max_N, :hp.max_T] ## TODO: check adding penalty here (1.0 is the right thing)               
        self.attention_masks = tf.to_float(tf.not_equal(self.A, -1))
        self.loss_att = tf.reduce_sum(tf.abs(self.A * self.gts) * self.attention_masks)    ## (B, Letters, Frames) * (Letters, Frames) -- Broadcasting first adds singleton dimensions to the left until rank is matched. 
        self.mask_sum = tf.reduce_sum(self.attention_masks)
        self.loss_att /= self.mask_sum

        # total loss

        ## loss weights
        self.lw_mel = hp.lw_mel
        self.lw_bd1 = hp.lw_bd1
        self.lw_att = hp.lw_att                                            
        
        self.loss = (self.lw_mel * self.loss_mels) + (self.lw_bd1 * self.loss_bd1) + (self.lw_att * self.loss_att)

        self.loss_components = [self.loss, self.loss_mels, self.loss_bd1, self.loss_att]

        tf.summary.scalar('train/loss_mels', self.loss_mels)
        tf.summary.scalar('train/loss_bd1', self.loss_bd1)
        tf.summary.scalar('train/loss_att', self.loss_att)
        tf.summary.image('train/mel_gt', tf.expand_dims(tf.transpose(self.mels[:1], [0, 2, 1]), -1))
        tf.summary.image('train/mel_hat', tf.expand_dims(tf.transpose(self.Y[:1], [0, 2, 1]), -1))


class BabblerGraph(Graph):
    def get_batchsize(self):
        pass