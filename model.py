import numpy as np
import imageio
import tensorflow as tf
import os

from math import floor, sin, pi
from glob import glob
from tqdm import tqdm

@tf.custom_gradient
def quantize(latent, codebook):
    # x     batch * width * height *        latent
    # c                              code * latent
    # r     batch * width * height * code

    index = tf.argmin(
        tf.reduce_sum((tf.expand_dims(latent, -2) - codebook)**2, [-1]), -1
    )
    code = tf.gather(codebook, index)
    quantized = code

    def grad(d_quantized, d_code):
        return d_quantized, tf.gradients(code, codebook, d_code)

    return [quantized, code], grad

class GANSuperResolution:
    def __init__(
        self, session, continue_train = True, 
        learning_rate = 1e-3,
        batch_size = 16
    ):
        self.session = session
        self.learning_rate = learning_rate
        self.batch_size = batch_size
        self.continue_train = continue_train
        self.filters = 64
        self.checkpoint_path = "checkpoints"
        self.size = 64
        
        self.global_step = tf.Variable(0, name = 'global_step')

        # build model
        print("lookup training data...")
        self.paths = glob("data/cropped/*.png")
                    
        def load(path):
            image = tf.image.decode_image(tf.read_file(path), 4)
            return tf.random_crop(image, [self.size + 10, self.size + 10, 4])
            
            
        lanczos3 = [
            3 * sin(pi * x) * sin(pi * x / 3) / pi**2 / x**2
            for x in np.linspace(-2.75, 2.75, 12)
        ]
        lanczos3 = [x / sum(lanczos3) for x in lanczos3]
        self.lanczos3_horizontal = tf.constant(
            [
                [[
                    [a if o == i else 0 for o in range(4)]
                    for i in range(4)
                ]] 
                for a in lanczos3
            ]
        )
        self.lanczos3_vertical = tf.constant(
            [[
                [
                    [a if o == i else 0 for o in range(4)]
                    for i in range(4)
                ]
                for a in lanczos3
            ]]
        )   
        
        d = tf.data.Dataset.from_tensor_slices(tf.constant(self.paths))
        d = d.shuffle(100000).repeat()
        d = d.map(load)
        d = d.batch(self.batch_size).prefetch(100)
        
        iterator = d.make_one_shot_iterator()
        self.real = iterator.get_next()
        
        real = self.srgb2xyz(self.real)
        downscaled = self.lanczos3_downscale(real, "VALID")

        # remove padding
        real = real[:, 5:-5, 5:-5, :]
        self.real = self.real[:, 5:-5, 5:-5, :]

        #downscaled += tf.random_normal(tf.shape(downscaled)) * 0.01
        self.downscaled = self.xyz2srgb(downscaled)

        self.tampered = tf.concat(
            [
                tf.map_fn(
                    lambda x: tf.image.random_jpeg_quality(x, 80, 100),
                    self.downscaled[:, :, :, :3]
                ),
                self.downscaled[:, :, :, 3:4] # keep alpha channel
            ], -1
        )
        tampered = self.srgb2xyz(self.tampered)
        
        self.nearest_neighbor = tf.image.resize_nearest_neighbor(
            self.downscaled, [self.size] * 2
        )
        
        
        codebook = tf.get_variable(
            'codebook', [16, 24],
            initializer = tf.initializers.random_normal()
        )
        
        encoded = self.encode(real)
        quantized, code = quantize(encoded, codebook)
        decoded = self.decode(downscaled, quantized)
        
        # losses
        self.loss = sum([
            tf.reduce_mean(tf.squared_difference(real, decoded)),
            tf.reduce_mean(tf.squared_difference(tf.stop_gradient(encoded), code)),
            0.01 * tf.reduce_mean(tf.squared_difference(encoded, tf.stop_gradient(code))),
        ])
        
        #optimizer = tf.train.GradientDescentOptimizer(self.learning_rate)
        optimizer = tf.train.AdamOptimizer()
        #optimizer = tf.train.MomentumOptimizer(learning_rate, 0.9, use_nesterov = True)
        #optimizer = tf.contrib.opt.AddSignOptimizer()
        
        self.optimizer = optimizer.minimize(self.loss, self.global_step)
        
        self.saver = tf.train.Saver(max_to_keep = 2)


        example_path = "example.png"
        example = self.srgb2xyz([tf.random_crop(
            tf.image.decode_image(tf.read_file(example_path), 4), 
            [175, 175, 4]
        )])
        example = self.xyz2srgb(self.decode(
            example, 
            tf.gather(
                codebook, 
                tf.random_uniform(
                    example.shape[:-1], 0, codebook.shape[0], tf.int32
                )
            )
        ))
        
        
        tf.summary.scalar('loss', self.loss)
        tf.summary.scalar(
            'latent variance', 
            tf.reduce_mean(tf.sqrt(tf.nn.moments(encoded, [0, 1, 2])[1]))
        )
        tf.summary.scalar(
            'code book variance', 
            tf.reduce_mean(tf.sqrt(tf.nn.moments(codebook, [0])[1]))
        )
        tf.summary.image(
            'kernel', 
            tf.transpose(
                tf.trainable_variables("transform/deconv0/kernel")[0][:, :, :, :3], 
                [2, 0, 1, 3]
            ),
            48
        )
        tf.summary.image('example', example)
        
        self.summary_writer = tf.summary.FileWriter('logs', self.session.graph)
        self.summary = tf.summary.merge_all()

        self.session.run(tf.global_variables_initializer())

        # load checkpoint
        if self.continue_train:
            print(" [*] Reading checkpoint...")

            checkpoint = tf.train.get_checkpoint_state(self.checkpoint_path)

            if checkpoint and checkpoint.model_checkpoint_path:
                self.saver.restore(
                    self.session,
                    self.checkpoint_path + "/" + os.path.basename(
                        checkpoint.model_checkpoint_path
                    )
                )
                print(" [*] before training, Load SUCCESS ")

            else:
                print(" [!] before training, failed to load ")
        else:
            print(" [!] before training, no need to load ")
            
            
    def lanczos3_downscale(self, x, padding = "SAME"):
        return tf.nn.conv2d(
            tf.nn.conv2d(
                x, 
                self.lanczos3_horizontal, [1, 2, 1, 1], padding
            ), 
            self.lanczos3_vertical, [1, 1, 2, 1], padding
        )
    def lanczos3_upscale(self, x):
        result = tf.nn.conv2d_transpose(
            tf.nn.conv2d_transpose(
                x * 4,
                self.lanczos3_horizontal, 
                tf.shape(x) * [1, 2, 1, 1],
                [1, 2, 1, 1], "SAME"
            ), 
            self.lanczos3_vertical, 
            tf.shape(x) * [1, 2, 2, 1], 
            [1, 1, 2, 1], "SAME"
        )
        result.set_shape(
            [x.shape[0], x.shape[1] * 2, x.shape[2] * 2, x.shape[3]]
        )
        return result
            
    def srgb2xyz(self, c):
        c = (tf.cast(c, tf.float32) + tf.random_uniform(tf.shape(c))) / 256
        c, alpha = tf.split(c, [3, 1], -1)
        c = c * alpha # pre-multiply
        linear = tf.where(
            c <= 0.04045,
            c / 12.92,
            ((c + 0.055) / 1.055)**2.4
        )
        return tf.concat(
            [
                linear * [[[[0.4124564, 0.2126729, 0.0193339]]]] +
                linear * [[[[0.3575761, 0.7151522, 0.1191920]]]] +
                linear * [[[[0.1804375, 0.0721750, 0.9503041]]]],
                alpha
            ], -1
        )
        
    def xyz2srgb(self, xyza):
        xyz, alpha = tf.split(xyza, [3, 1], -1)
        linear = (
            xyz * [[[[3.2404542, -0.9692660, 0.0556434]]]] +
            xyz * [[[[-1.5371385, 1.8760108, -0.2040259]]]] +
            xyz * [[[[-0.4985314, 0.0415560, 1.0572252]]]]
        )
        srgb = tf.where(
            linear <= 0.003131,
            12.92 * linear,
            1.055 * linear**(1 / 2.4) - 0.055
        )
        #srgb = nice_power(linear, 1 / 2.4, 0.003131, 2) - 0.055
        srgb = srgb / tf.maximum(alpha, 1 / 256)
        srgb = tf.concat([srgb, alpha], -1)
        return tf.cast(tf.minimum(tf.maximum(
            srgb * 256, 0
        ), 255), tf.uint8)

    def decode(self, small_images, latent):
        with tf.variable_scope(
            'transform', reuse = tf.AUTO_REUSE
        ):
            x = small_images * 2 - 1

            x = tf.concat([x, latent], -1)

            x = tf.layers.conv2d_transpose(
                x, 48,
                [16, 16], [2, 2], 'same', name = 'deconv0', use_bias = False
            )
            x = tf.layers.dense(
                x, self.filters, name = 'dense0'
            )

            x = tf.nn.relu(x)
            
            sample = tf.layers.dense(
                x, 4, name = 'dense1'
            ) * 0.5 + 0.5

            return sample
            
    def encode(self, large_images):
        with tf.variable_scope(
            'discriminate', reuse = tf.AUTO_REUSE
        ):
            large_images = large_images * 2 - 1

            x = tf.layers.conv2d(
                large_images, 48,
                [8, 8], [2, 2], 'same', name = 'conv0'#, use_bias = False
            )
            x = tf.layers.dense(
                x, self.filters, name = 'dense0'
            )
            
            x = tf.nn.relu(x)
            
            x = tf.layers.dense(x, 24, name = 'dense1')

            return x
 
    def train(self):
        step = self.session.run(self.global_step)
        
        while True:
            while True:
                try:
                    summary = self.session.run(self.summary)
                    break
                except tf.errors.InvalidArgumentError as e:
                    print(e.message)
        
            self.summary_writer.add_summary(summary, step)
                
            for _ in tqdm(range(1000)):
                while True:
                    try:
                        _, step = self.session.run([
                            [
                                self.optimizer
                            ],
                            self.global_step
                        ])
                        break
                    except tf.errors.InvalidArgumentError as e:
                        print(e.message)
                
            if step % 16000 == 0:
                pass
                print("saving iteration " + str(step))
                self.saver.save(
                    self.session,
                    self.checkpoint_path + "/gansr",
                    global_step=step
                )

    def scale_file(self, filename):
        image = tf.Variable(
            tf.image.decode_image(tf.read_file(filename), 3),
            validate_shape = False
        )
        
        tiles = tf.Variable(
            tf.reshape(
                tf.extract_image_patches(
                    [tf.pad(
                        image, [[0, 0], [0, 0], [0, 1]], 
                        constant_values = 255
                    )], 
                    [1, 128, 128, 1], [1, 128, 128, 1], [1, 1, 1, 1], "SAME"
                ), 
                [-1, 128, 128, 4]
            ),
            validate_shape = False
        )
        
        result_tiles = tf.Variable(
            tf.zeros(tf.shape(tiles) * [1, 2, 2, 1], tf.int32),
            validate_shape = False
        )
        
        index = tf.Variable(0)
        
        self.session.run(image.initializer)
        self.session.run([
            tiles.initializer, result_tiles.initializer, index.initializer
        ])
        
        size = self.session.run(tf.shape(image)[:2])
        print(size)
        
        step = tf.scatter_update(
            result_tiles, [index], 
            self.xyz2srgb(
                self.scale(self.srgb2xyz(
                    tf.reshape([tiles[index, :, :, :]], [1, 128, 128, 4])
                ))
            )
        )
        
        with tf.control_dependencies([step]):
            step = tf.assign_add(index, 1)
    
        tile_count = self.session.run(tf.shape(tiles)[0])
        
        for i in tqdm(range(tile_count)):
            self.session.run(step)
            
        height = ((size[0] - 1) // 128 + 1) * 256
        width =  ((size[1] - 1) // 128 + 1) * 256
            
        r = self.session.run(
            tf.reshape(
                tf.transpose(
                    tf.reshape(
                        tf.transpose(result_tiles, [0, 2, 1, 3]),
                        [-1, width, 256, 4]
                    ), 
                    [0, 2, 1, 3]
                ),
                [-1, width, 4]
            )
        )

        imageio.imwrite("{}_scaled.png".format(filename), r)
        
    