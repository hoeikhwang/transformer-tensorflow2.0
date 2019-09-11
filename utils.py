import os
import tensorflow as tf

class Mask:
    @classmethod
    def create_padding_mask(cls, sequences):
        sequences = tf.cast(tf.math.equal(sequences, 0), dtype=tf.float32)
        return sequences[:, tf.newaxis, tf.newaxis, :]
    
    @classmethod
    def create_look_ahead_mask(cls, seq_len):
        return 1 - tf.linalg.band_part(tf.ones((seq_len, seq_len)), -1, 0)
    
    @classmethod
    def create_masks(cls, input, target):
        encoder_padding_mask = Mask.create_padding_mask(input)
        decoder_padding_mask = Mask.create_padding_mask(input)
        
        look_ahead_mask = tf.maximum(
            Mask.create_look_ahead_mask(tf.shape(target)[1]),
            Mask.create_padding_mask(target)
            )
        
        return encoder_padding_mask, look_ahead_mask, decoder_padding_mask


class CustomSchedule(tf.keras.optimizers.schedules.LearningRateSchedule):
    def __init__(self, d_model, warmup_steps=4000):
        super(CustomSchedule, self).__init__()
        self.d_model = d_model
        self.d_model = tf.cast(self.d_model, tf.float32)

        self.warmup_steps = warmup_steps

    def __call__(self, step):
        arg1 = tf.math.rsqrt(step)
        arg2 = step * (self.warmup_steps ** -1.5)

        return tf.math.rsqrt(self.d_model) * tf.math.minimum(arg1, arg2)


class Trainer:
    def __init__(self, model, loss_object, optimizer, checkpoint_dir='./checkpoints', batch_size=None, distribute_strategy=None):
        self.batch_size = batch_size
        self.distribute_strategy = distribute_strategy
        self.model = model
        self.loss_object = loss_object
        self.optimizer = optimizer
        self.checkpoint_dir = checkpoint_dir
        os.makedirs(self.checkpoint_dir, exist_ok=True)
        #         self.checkpoint = tf.train.Checkpoint(optimizer=self.optimizer, model=self.model)
        self.checkpoint = tf.train.Checkpoint(step=tf.Variable(1), optimizer=self.optimizer, model=self.model)
        self.checkpoint_manager = tf.train.CheckpointManager(self.checkpoint, self.checkpoint_dir, max_to_keep=3)

        # metrics
        self.train_loss = tf.keras.metrics.Mean('train_loss', dtype=tf.float32)
        self.train_accuracy = tf.keras.metrics.SparseCategoricalAccuracy('train_accuracy')
        self.validation_loss = tf.keras.metrics.Mean('validation_loss', dtype=tf.float32)
        self.validation_accuracy = tf.keras.metrics.SparseCategoricalAccuracy('validation_accuracy')

    def distributed_train_step(self, input, target):
        loss = self.distribute_strategy.experimental_run_v2(self.train_step, args=(input, target))
        loss_value = self.distribute_strategy.reduce(tf.distribute.ReduceOp.MEAN, loss, axis=None)
        return tf.reduce_mean(loss_value)

    def train_step(self, input, target):
        target_include_start = target[:, :-1]
        target_include_end = target[:, 1:]
        encoder_padding_mask, look_ahead_mask, decoder_padding_mask = Mask.create_masks(
            input, target_include_start
        )

        with tf.GradientTape() as tape:
            pred = self.model.call(
                input=input,
                target=target_include_start,
                input_padding_mask=encoder_padding_mask,
                look_ahead_mask=look_ahead_mask,
                target_padding_mask=decoder_padding_mask,
                training=True
            )
            if self.distribute_strategy is None:
                loss = self.loss_function(target_include_end, pred)
            else:
                loss = self.distributed_loss_function(target_include_end, pred)
        gradients = tape.gradient(loss, self.model.trainable_variables)
        self.optimizer.apply_gradients(zip(gradients, self.model.trainable_variables))

        self.train_loss(loss)
        self.train_accuracy(target_include_end, pred)
        if self.distribute_strategy is None:
            return tf.reduce_mean(loss)
        else:
            return loss

    def loss_function(self, real, pred):
        mask = tf.math.logical_not(tf.math.equal(real, 0))
        loss = self.loss_object(real, pred)

        mask = tf.cast(mask, dtype=loss.dtype)

        loss *= mask
        return tf.reduce_mean(loss)

    def distributed_loss_function(self, real, pred):
        mask = tf.math.logical_not(tf.math.equal(real, 0))
        loss = self.loss_object(real, pred)

        mask = tf.cast(mask, dtype=loss.dtype)

        loss *= mask
        return tf.reduce_mean(loss) / (1.0 / self.batch_size)
