"""
Copyright: Intel Corp. 2018
Author: Wenyi Tang
Email: wenyi.tang@intel.com
Created Date: Oct 15th 2018

Improved train/benchmark/infer script
"""

import tensorflow as tf

from VSR.DataLoader.Dataset import load_datasets, Dataset
from VSR.DataLoader.Loader import QuickLoader
from VSR.Models import get_model, list_supported_models
from VSR.Util.Config import Config
from VSR.Framework.Callbacks import *

try:
    from .custom_api import *
except ImportError:
    from custom_api import *

tf.flags.DEFINE_enum('model', None, list_supported_models(), help="specify a model to use")
tf.flags.DEFINE_enum('output_color', 'RGB', ('RGB', 'L', 'GRAY', 'Y'), help="specify output color format")
tf.flags.DEFINE_integer('epochs', 50, lower_bound=1, help="training epochs")
tf.flags.DEFINE_integer('steps_per_epoch', 200, lower_bound=1, help="specify steps in every epoch training")
tf.flags.DEFINE_integer('threads', 1, lower_bound=1, help="number of threads to use while reading data")
tf.flags.DEFINE_integer('output_index', -1, help="specify access index of output array")
tf.flags.DEFINE_string('test', None, help="specify another dataset for testing")
tf.flags.DEFINE_string('infer', None, help="specify a file, a path or a dataset for inferring")
tf.flags.DEFINE_string('save_dir', '../Results', help="specify a folder to save checkpoint and output images")
tf.flags.DEFINE_string('data_config', '../Data/datasets.yaml', help="path to data config file")
tf.flags.DEFINE_string('dataset', None, help="specify a dataset alias for training")
tf.flags.DEFINE_string('memory_limit', None, help="limit the memory usage. i.e. '4GB', '1024MB'")
tf.flags.DEFINE_string('comment', None, help="append a postfix string to save dir")
tf.flags.DEFINE_multi_string('add_custom_callbacks', None, help="")
tf.flags.DEFINE_bool('export', False, help="whether to export tf model")
tf.flags.DEFINE_bool('v', False, help="show verbose")
tf.flags.DEFINE_bool('cifar', False, help="temp use, delete if not need")  # TODO delete me


def check_args(opt):
    _required = ('model', 'dataset')
    for r in _required:
        if r not in opt or not opt.get(r):
            raise ValueError('--' + r + ' must be set')


def fetch_datasets(data_config_file, opt):
    all_datasets = load_datasets(data_config_file)
    dataset = all_datasets[opt.dataset.upper()]
    if opt.test:
        test_data = all_datasets[opt.test.upper()]
    else:
        test_data = dataset
    if opt.infer:
        infer_dir = Path(opt.infer)
        if infer_dir.exists():
            # infer files in this directory
            if infer_dir.is_file():
                images = [str(infer_dir)]
            else:
                images = list(infer_dir.glob('*'))
                if not images:
                    images = infer_dir.iterdir()
            infer_data = Dataset(infer=images, mode='pil-image1', modcrop=False)
        else:
            infer_data = all_datasets[opt.infer.upper()]
    else:
        infer_data = test_data
    # TODO temp use, delete if not need
    if opt.cifar:
        cifar_data, cifar_test = tf.keras.datasets.cifar10.load_data()
        dataset = Dataset(**dataset)
        dataset.mode = 'numpy'
        dataset.train = [cifar_data[0]]
        dataset.val = [cifar_test[0]]
    return dataset, test_data, infer_data


def init_loader_config(opt):
    train_config = Config(**opt, crop='random', feature_callbacks=[], label_callbacks=[])
    benchmark_config = Config(**opt, crop=None, feature_callbacks=[], label_callbacks=[], output_callbacks=[])
    infer_config = Config(**opt, feature_callbacks=[], label_callbacks=[], output_callbacks=[])
    benchmark_config.batch = 1
    benchmark_config.steps_per_epoch = -1
    if opt.channel == 1:
        train_config.convert_to = 'gray'
        benchmark_config.convert_to = 'yuv'
        benchmark_config.feature_callbacks = train_config.feature_callbacks + [to_gray()]
        benchmark_config.label_callbacks = train_config.label_callbacks + [to_gray()]
        if opt.output_color == 'RGB':
            benchmark_config.output_callbacks = [to_rgb()]
        benchmark_config.output_callbacks += [save_image(opt.root, opt.output_index)]
        infer_config.update(benchmark_config)
    else:
        train_config.convert_to = 'rgb'
        benchmark_config.convert_to = 'rgb'
        benchmark_config.output_callbacks += [save_image(opt.root, opt.output_index)]
        infer_config.update(benchmark_config)
    if opt.add_custom_callbacks is not None:
        for fn in opt.add_custom_callbacks:
            train_config.feature_callbacks += [globals()[fn]]
    # modcrop: A boolean to specify whether to crop the edge of images to be divisible
    #          by `scale`. It's useful when to provide batches with original shapes.
    infer_config.modcrop = False
    return train_config, benchmark_config, infer_config


def main(*args):
    flags = tf.flags.FLAGS
    opt = Config()
    for key in flags:
        opt.setdefault(key, flags.get_flag_value(key, None))
    check_args(opt)
    data_config_file = Path(opt.data_config)
    if not data_config_file.exists():
        raise RuntimeError("dataset config file doesn't exist!")
    for _suffix in ('json', 'yaml'):
        # apply a 2-stage (or master-slave) configuration, master can be override by slave
        model_config_root = Path('parameters/{}.{}'.format('root', _suffix))
        model_config_file = Path('parameters/{}.{}'.format(opt.model, _suffix))
        if model_config_root.exists():
            opt.update(Config(str(model_config_root)))
        if model_config_file.exists():
            opt.update(Config(str(model_config_file)))

    model_params = opt.get(opt.model)
    opt.update(model_params)
    model = get_model(opt.model)(**model_params)
    root = '{}/{}_sc{}_c{}'.format(opt.save_dir, model.name, opt.scale, opt.channel)
    if opt.comment:
        root += '_' + opt.comment
    opt.root = root
    verbosity = tf.logging.DEBUG if opt.v else tf.logging.INFO
    # map model to trainer, ~~manually~~ automatically, by setting _trainer attribute in models
    trainer = model.trainer
    train_data, test_data, infer_data = fetch_datasets(data_config_file, opt)
    train_config, test_config, infer_config = init_loader_config(opt)
    test_config.subdir = test_data.name
    # start fitting!
    with trainer(model, root, verbosity) as t:
        # prepare loader
        loader = partial(QuickLoader, n_threads=opt.threads)
        train_loader = loader(train_data, 'train', train_config, augmentation=True)
        val_loader = loader(train_data, 'val', train_config, augmentation=True, crop='center', steps_per_epoch=1)
        test_loader = loader(test_data, 'test', test_config)
        infer_loader = loader(infer_data, 'infer', infer_config)
        # fit
        t.fit([train_loader, val_loader], train_config)
        # validate
        t.benchmark(test_loader, test_config)
        # do inference
        t.infer(infer_loader, infer_config)
        if opt.export:
            t.export(opt.root)


if __name__ == '__main__':
    tf.app.run(main)
