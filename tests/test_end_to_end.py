import torch as t
from nnsight import LanguageModel
import os
import json
import random

from dictionary_learning.training import trainSAE
from dictionary_learning.trainers.standard import StandardTrainer
from dictionary_learning.trainers.top_k import TrainerTopK, AutoEncoderTopK
from dictionary_learning.utils import hf_dataset_to_generator
from dictionary_learning.buffer import ActivationBuffer
from dictionary_learning.dictionary import (
    AutoEncoder,
    GatedAutoEncoder,
    AutoEncoderNew,
    JumpReluAutoEncoder,
)
from dictionary_learning.evaluation import evaluate

EXPECTED_RESULTS = {
    "AutoEncoderTopK": {
        "l2_loss": 4.470372676849365,
        "l1_loss": 44.47749710083008,
        "l0": 40.0,
        "frac_alive": 0.000244140625,
        "frac_variance_explained": 0.9372208118438721,
        "cossim": 0.9471381902694702,
        "l2_ratio": 0.9523985981941223,
        "relative_reconstruction_bias": 0.9996458888053894,
        "loss_original": 3.186223268508911,
        "loss_reconstructed": 3.690929412841797,
        "loss_zero": 12.936649322509766,
        "frac_recovered": 0.9482374787330627,
    },
    "AutoEncoder": {
        "l2_loss": 6.72230863571167,
        "l1_loss": 28.893749237060547,
        "l0": 61.12999725341797,
        "frac_alive": 0.000244140625,
        "frac_variance_explained": 0.6076533794403076,
        "cossim": 0.869738757610321,
        "l2_ratio": 0.8005934953689575,
        "relative_reconstruction_bias": 0.9304398894309998,
        "loss_original": 3.186223268508911,
        "loss_reconstructed": 5.501500129699707,
        "loss_zero": 12.936649322509766,
        "frac_recovered": 0.7625460624694824,
    },
}

DEVICE = "cuda:0"
SAVE_DIR = "./test_data"
MODEL_NAME = "EleutherAI/pythia-70m-deduped"
RANDOM_SEED = 42
LAYER = 3
DATASET_NAME = "monology/pile-uncopyrighted"

EVAL_TOLERANCE = 0.01


def get_nested_folders(path: str) -> list[str]:
    """
    Recursively get a list of folders that contain an ae.pt file, starting the search from the given path
    """
    folder_names = []

    for root, dirs, files in os.walk(path):
        if "ae.pt" in files:
            folder_names.append(root)

    return folder_names


def load_dictionary(base_path: str, device: str) -> tuple:
    ae_path = f"{base_path}/ae.pt"
    config_path = f"{base_path}/config.json"

    with open(config_path, "r") as f:
        config = json.load(f)

    # TODO: Save the submodule name in the config?
    # submodule_str = config["trainer"]["submodule_name"]
    dict_class = config["trainer"]["dict_class"]

    if dict_class == "AutoEncoder":
        dictionary = AutoEncoder.from_pretrained(ae_path, device=device)
    elif dict_class == "GatedAutoEncoder":
        dictionary = GatedAutoEncoder.from_pretrained(ae_path, device=device)
    elif dict_class == "AutoEncoderNew":
        dictionary = AutoEncoderNew.from_pretrained(ae_path, device=device)
    elif dict_class == "AutoEncoderTopK":
        k = config["trainer"]["k"]
        dictionary = AutoEncoderTopK.from_pretrained(ae_path, k=k, device=device)
    elif dict_class == "JumpReluAutoEncoder":
        dictionary = JumpReluAutoEncoder.from_pretrained(ae_path, device=device)
    else:
        raise ValueError(f"Dictionary class {dict_class} not supported")

    return dictionary, config


def test_sae_training():
    """End to end test for training an SAE. Takes ~3 minutes on an RTX 3090.
    This isn't a nice suite of unit tests, but it's better than nothing."""
    random.seed(RANDOM_SEED)
    t.manual_seed(RANDOM_SEED)

    MODEL_NAME = "EleutherAI/pythia-70m-deduped"
    model = LanguageModel(MODEL_NAME, dispatch=True, device_map=DEVICE)
    layer = 3

    context_length = 128
    llm_batch_size = 512  # Fits on a 24GB GPU
    sae_batch_size = 8192
    num_contexts_per_sae_batch = sae_batch_size // context_length

    num_inputs_in_buffer = num_contexts_per_sae_batch * 20

    num_tokens = 10_000_000

    # sae training parameters
    random_seed = 42
    k = 40
    sparsity_penalty = 0.05
    expansion_factor = 8

    steps = int(num_tokens / sae_batch_size)  # Total number of batches to train
    save_steps = None
    warmup_steps = 1000  # Warmup period at start of training and after each resample
    resample_steps = None

    # standard sae training parameters
    learning_rate = 3e-4

    # topk sae training parameters
    decay_start = 24000
    auxk_alpha = 1 / 32

    submodule = model.gpt_neox.layers[LAYER]
    submodule_name = f"resid_post_layer_{LAYER}"
    io = "out"
    activation_dim = model.config.hidden_size

    generator = hf_dataset_to_generator(DATASET_NAME)

    activation_buffer = ActivationBuffer(
        generator,
        model,
        submodule,
        n_ctxs=num_inputs_in_buffer,
        ctx_len=context_length,
        refresh_batch_size=llm_batch_size,
        out_batch_size=sae_batch_size,
        io=io,
        d_submodule=activation_dim,
        device=DEVICE,
    )

    # create the list of configs
    trainer_configs = []
    trainer_configs.extend(
        [
            {
                "trainer": TrainerTopK,
                "dict_class": AutoEncoderTopK,
                "activation_dim": activation_dim,
                "dict_size": expansion_factor * activation_dim,
                "k": k,
                "auxk_alpha": auxk_alpha,  # see Appendix A.2
                "decay_start": decay_start,  # when does the lr decay start
                "steps": steps,  # when when does training end
                "seed": random_seed,
                "wandb_name": f"TopKTrainer-{MODEL_NAME}-{submodule_name}",
                "device": DEVICE,
                "layer": layer,
                "lm_name": MODEL_NAME,
                "submodule_name": submodule_name,
            },
        ]
    )
    trainer_configs.extend(
        [
            {
                "trainer": StandardTrainer,
                "dict_class": AutoEncoder,
                "activation_dim": activation_dim,
                "dict_size": expansion_factor * activation_dim,
                "lr": learning_rate,
                "l1_penalty": sparsity_penalty,
                "warmup_steps": warmup_steps,
                "resample_steps": resample_steps,
                "seed": random_seed,
                "wandb_name": f"StandardTrainer-{MODEL_NAME}-{submodule_name}",
                "layer": layer,
                "lm_name": MODEL_NAME,
                "device": DEVICE,
                "submodule_name": submodule_name,
            },
        ]
    )

    print(f"len trainer configs: {len(trainer_configs)}")
    output_dir = f"{SAVE_DIR}/{submodule_name}"

    trainSAE(
        data=activation_buffer,
        trainer_configs=trainer_configs,
        steps=steps,
        save_steps=save_steps,
        save_dir=output_dir,
    )

    folders = get_nested_folders(output_dir)

    assert len(folders) == 2

    for folder in folders:
        dictionary, config = load_dictionary(folder, DEVICE)

        assert dictionary is not None
        assert config is not None


def test_evaluation():
    random.seed(RANDOM_SEED)
    t.manual_seed(RANDOM_SEED)

    model = LanguageModel(MODEL_NAME, dispatch=True, device_map=DEVICE)
    ae_paths = get_nested_folders(SAVE_DIR)

    context_length = 128
    llm_batch_size = 100
    buffer_size = 256
    io = "out"

    generator = hf_dataset_to_generator(DATASET_NAME)
    submodule = model.gpt_neox.layers[LAYER]

    input_strings = []
    for i, example in enumerate(generator):
        input_strings.append(example)
        if i > buffer_size * 2:
            break

    for ae_path in ae_paths:
        dictionary, config = load_dictionary(ae_path, DEVICE)
        dictionary = dictionary.to(dtype=model.dtype)

        activation_dim = config["trainer"]["activation_dim"]
        context_length = config["buffer"]["ctx_len"]

        activation_buffer_data = iter(input_strings)

        activation_buffer = ActivationBuffer(
            activation_buffer_data,
            model,
            submodule,
            n_ctxs=buffer_size,
            ctx_len=context_length,
            refresh_batch_size=llm_batch_size,
            out_batch_size=llm_batch_size,
            io=io,
            d_submodule=activation_dim,
            device=DEVICE,
        )

        eval_results = evaluate(
            dictionary,
            activation_buffer,
            context_length,
            llm_batch_size,
            io=io,
            device=DEVICE,
        )

        print(eval_results)

        dict_class = config["trainer"]["dict_class"]
        expected_results = EXPECTED_RESULTS[dict_class]

        for key, value in expected_results.items():
            assert abs(eval_results[key] - value) < EVAL_TOLERANCE