import argparse
import shutil
import os
import six

from ctranslate2.converters import utils
from ctranslate2.converters.converter import Converter
from ctranslate2.specs import catalog, transformer_spec


def load_model(model_dir, src_vocab=None, tgt_vocab=None):
    """Loads variables and vocabularies from a TensorFlow checkpoint or SavedModel."""
    import tensorflow as tf
    try:
        from tensorflow.contrib.seq2seq.python.ops import beam_search_ops  # Force kernel loading.
    except ImportError:
        pass

    model_version = 1
    tf_version = int(tf.version.VERSION[0])
    if tf.saved_model.contains_saved_model(model_dir):
        if tf_version == 2:
            raise NotImplementedError("Converting SavedModel with TensorFlow 2.0 is "
                                      "currently not implemented")
        elif tf_version == 1:
            config = tf.compat.v1.ConfigProto(device_count={'GPU': 0})
            with tf.compat.v1.Graph().as_default():
                with tf.compat.v1.Session(config=config) as sess:
                    meta_graph = tf.compat.v1.saved_model.loader.load(sess, ["serve"], model_dir)
                    variables = sess.run(
                        {variable.op.name:variable for variable in tf.compat.v1.global_variables()})
                    assets = sess.run(tf.compat.v1.get_collection(tf.GraphKeys.ASSET_FILEPATHS))
            src_vocab = os.path.join(six.b(model_dir), b"assets", os.path.basename(assets[0]))
            tgt_vocab = os.path.join(six.b(model_dir), b"assets", os.path.basename(assets[1]))
        else:
            raise ValueError("Unsupported TensorFlow version %d" % tf_version)
    else:
        if src_vocab is None or tgt_vocab is None:
            raise ValueError("vocabularies must be passed as argument when converting checkpoint")
        checkpoint = tf.train.latest_checkpoint(model_dir)
        reader = tf.train.load_checkpoint(checkpoint)
        variables = {
            name:reader.get_tensor(name)
            for name in six.iterkeys(reader.get_variable_to_shape_map())}
        if os.path.basename(checkpoint).startswith("ckpt"):
            model_version = 2
            variables = {
                name.replace("/.ATTRIBUTES/VARIABLE_VALUE", ""):value
                for name, value in six.iteritems(variables)}
    return model_version, variables, src_vocab, tgt_vocab


class OpenNMTTFConverter(Converter):
    """Converts models generated by OpenNMT-tf."""

    def __init__(self, model_dir, src_vocab=None, tgt_vocab=None):
        self._model_dir = model_dir
        self._src_vocab = src_vocab
        self._tgt_vocab = tgt_vocab

    def _load(self, model_spec):
        version, variables, src_vocab, tgt_vocab = load_model(
            self._model_dir,
            src_vocab=self._src_vocab,
            tgt_vocab=self._tgt_vocab)
        if isinstance(model_spec, (catalog.TransformerBase, catalog.TransformerBig)):
            if version == 2:
                set_transformer_spec_v2(model_spec, variables)
            else:
                set_transformer_spec(model_spec, variables)
        else:
            raise NotImplementedError()
        return src_vocab, tgt_vocab

    def _save_vocabulary(self, vocab, destination):
        shutil.copy(vocab, destination)


def set_transformer_spec_v2(spec, variables):
    set_transformer_encoder_v2(spec.encoder, variables, "model/encoder")
    set_transformer_decoder_v2(spec.decoder, variables, "model/decoder")
    set_embeddings(
        spec.encoder.embeddings, variables, "model/examples_inputter/features_inputter", version=2)
    set_embeddings(
        spec.decoder.embeddings, variables, "model/examples_inputter/labels_inputter", version=2)

def set_transformer_encoder_v2(spec, variables, scope):
    set_layer_norm(spec.layer_norm, variables, "%s/layer_norm" % scope)
    for i, layer in enumerate(spec.layer):
        set_transformer_encoder_layer_v2(layer, variables, "%s/layers/%d" % (scope, i))

def set_transformer_decoder_v2(spec, variables, scope):
    set_linear(spec.projection, variables, "%s/output_layer" % scope)
    set_layer_norm(spec.layer_norm, variables, "%s/layer_norm" % scope)
    for i, layer in enumerate(spec.layer):
        set_transformer_decoder_layer_v2(layer, variables, "%s/layers/%d" % (scope, i))

def set_transformer_encoder_layer_v2(spec, variables, scope):
    set_ffn_v2(spec.ffn, variables, "%s/ffn" % scope)
    set_multi_head_attention_v2(
        spec.self_attention, variables, "%s/self_attention" % scope, self_attention=True)

def set_transformer_decoder_layer_v2(spec, variables, scope):
    set_ffn_v2(spec.ffn, variables, "%s/ffn" % scope)
    set_multi_head_attention_v2(
        spec.self_attention, variables, "%s/self_attention" % scope, self_attention=True)
    set_multi_head_attention_v2(
        spec.attention, variables, "%s/attention/0" % scope)

def set_ffn_v2(spec, variables, scope):
    set_layer_norm(spec.layer_norm, variables, "%s/input_layer_norm" % scope)
    set_linear(spec.linear_0, variables, "%s/layer/inner" % scope)
    set_linear(spec.linear_1, variables, "%s/layer/outer" % scope)

def set_multi_head_attention_v2(spec, variables, scope, self_attention=False):
    set_layer_norm(spec.layer_norm, variables, "%s/input_layer_norm" % scope)
    if self_attention:
        split_layers = [transformer_spec.LinearSpec() for _ in range(3)]
        set_linear(split_layers[0], variables, "%s/layer/linear_queries" % scope)
        set_linear(split_layers[1], variables, "%s/layer/linear_keys" % scope)
        set_linear(split_layers[2], variables, "%s/layer/linear_values" % scope)
        utils.fuse_linear(spec.linear[0], split_layers)
    else:
        set_linear(spec.linear[0], variables, "%s/layer/linear_queries" % scope)
        split_layers = [transformer_spec.LinearSpec() for _ in range(2)]
        set_linear(split_layers[0], variables, "%s/layer/linear_keys" % scope)
        set_linear(split_layers[1], variables, "%s/layer/linear_values" % scope)
        utils.fuse_linear(spec.linear[1], split_layers)
    set_linear(spec.linear[-1], variables, "%s/layer/linear_output" % scope)


def set_transformer_spec(spec, variables):
    set_transformer_encoder(spec.encoder, variables)
    set_transformer_decoder(spec.decoder, variables)

def set_transformer_encoder(spec, variables):
    set_layer_norm(spec.layer_norm, variables, "transformer/encoder/LayerNorm")
    set_embeddings(spec.embeddings, variables, "transformer/encoder")
    for i, layer in enumerate(spec.layer):
        set_transformer_encoder_layer(layer, variables, "transformer/encoder/layer_%d" % i)

def set_transformer_decoder(spec, variables):
    set_linear(spec.projection, variables, "transformer/decoder/dense")
    set_layer_norm(spec.layer_norm, variables, "transformer/decoder/LayerNorm")
    set_embeddings(spec.embeddings, variables, "transformer/decoder")
    for i, layer in enumerate(spec.layer):
        set_transformer_decoder_layer(layer, variables, "transformer/decoder/layer_%d" % i)

def set_transformer_encoder_layer(spec, variables, scope):
    set_ffn(spec.ffn, variables, "%s/ffn" % scope)
    set_multi_head_attention(
        spec.self_attention, variables, "%s/multi_head" % scope, self_attention=True)

def set_transformer_decoder_layer(spec, variables, scope):
    set_ffn(spec.ffn, variables, "%s/ffn" % scope)
    set_multi_head_attention(
        spec.self_attention, variables, "%s/masked_multi_head" % scope, self_attention=True)
    set_multi_head_attention(spec.attention, variables, "%s/multi_head" % scope)

def set_ffn(spec, variables, scope):
    set_layer_norm(spec.layer_norm, variables, "%s/LayerNorm" % scope)
    set_linear(spec.linear_0, variables, "%s/conv1d" % scope)
    set_linear(spec.linear_1, variables, "%s/conv1d_1" % scope)

def set_multi_head_attention(spec, variables, scope, self_attention=False):
    set_layer_norm(spec.layer_norm, variables, "%s/LayerNorm" % scope)
    set_linear(spec.linear[0], variables, "%s/conv1d" % scope)
    set_linear(spec.linear[1], variables, "%s/conv1d_1" % scope)
    if not self_attention:
        set_linear(spec.linear[2], variables, "%s/conv1d_2" % scope)

def set_layer_norm(spec, variables, scope):
    spec.gamma = variables["%s/gamma" % scope]
    spec.beta = variables["%s/beta" % scope]

def set_linear(spec, variables, scope):
    spec.weight = variables["%s/kernel" % scope].squeeze().transpose()
    spec.bias = variables["%s/bias" % scope]

def set_embeddings(spec, variables, scope, version=1):
    if version == 2:
        name = "embedding"
    else:
        name = "w_embs"
    spec.weight = variables["%s/%s" % (scope, name)]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--model_dir", required=True,
                        help="Model directory (a checkpoint directory or a SavedModel bundle).")
    parser.add_argument("--src_vocab", default=None,
                        help="Source vocabulary file (required if converting a checkpoint).")
    parser.add_argument("--tgt_vocab", default=None,
                        help="Target vocabulary file (required if converting a checkpoint).")
    Converter.declare_arguments(parser)
    args = parser.parse_args()
    OpenNMTTFConverter(
        args.model_dir,
        src_vocab=args.src_vocab,
        tgt_vocab=args.tgt_vocab).convert_from_args(args)
