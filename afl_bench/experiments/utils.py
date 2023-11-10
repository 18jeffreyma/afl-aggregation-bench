import argparse
import re
from functools import partial
from typing import Any, Dict, Tuple

import torch

import wandb
from afl_bench.agents.client_thread import ClientThread
from afl_bench.agents.clients.simple import Client
from afl_bench.agents.runtime_model import GaussianRuntime, InstantRuntime
from afl_bench.agents.server import Server
from afl_bench.agents.strategies import Strategy
from afl_bench.datasets.cifar10 import (
    load_cifar10_iid,
    load_cifar10_one_class_per_client,
    load_cifar10_randomly_remove,
    load_cifar10_sorted_partition,
)
from afl_bench.datasets.fashion_mnist import (
    load_fashion_mnist_iid,
    load_fashion_mnist_one_class_per_client,
    load_fashion_mnist_randomly_remove,
    load_fashion_mnist_sorted_partition,
)

CLIENT_TYPE_TO_MODEL = {
    "i": InstantRuntime,
    "g": GaussianRuntime,
}

# Always use CUDA if available, otherwise use MPS if available, otherwise use CPU.
DEVICE = (
    "cuda"
    if torch.cuda.is_available()
    else ("mps" if torch.backends.mps.is_available() else "cpu")
)


def get_cmd_line_parser() -> Dict[str, Any]:
    # Run parameters.
    parser = argparse.ArgumentParser(description="Run FedAvg on CIFAR-10")

    # Dataset parameters.
    parser.add_argument(
        "-d",
        "--dataset",
        help="Dataset to use",
        required=True,
        choices=["cifar10", "fashion_mnist"],
    )
    parser.add_argument(
        "-dd",
        "--data-distribution",
        help="Data distribution to use",
        required=True,
        choices=[
            "iid",
            "sorted_partition",
            "one_class_per_client",
            "randomly_remove",
        ],
    )
    parser.add_argument(
        "--num-remove",
        help="Number of classes to remove given randomly remove",
        type=int,
    )

    # Client parameters.
    parser.add_argument(
        "-ci", "--client-info", help="Clients by runtime", required=True, type=str
    )

    parser.add_argument(
        "-cns",
        "--client-num-steps",
        help="Number of client steps",
        default=10,
        type=int,
    )
    parser.add_argument(
        "-clr", "--client-lr", help="Client learning rate", default=0.001, type=float
    )

    parser.add_argument(
        "--batch-size",
        help="Number of server aggregations",
        default=32,
        type=int,
    )

    # Server parameters.
    parser.add_argument(
        "-wff", "--wait-for-full", help="Wait for full buffer", action="store_true"
    )
    parser.add_argument(
        "-bs", "--buffer-size", help="Buffer size", required=True, type=int
    )
    parser.add_argument(
        "-ms", "--ms-to-wait", help="Milliseconds to wait", required=False, type=int
    )
    parser.add_argument(
        "--num-aggregations",
        help="Number of server aggregations",
        required=True,
        type=int,
    )

    arguments = vars(parser.parse_args())

    # Choose dataset distribution load based on run config.
    load_functions = {
        "cifar10": {
            "iid": load_cifar10_iid,
            "one_class_per_client": load_cifar10_one_class_per_client,
            "sorted_partition": load_cifar10_sorted_partition,
            "randomly_remove": partial(
                load_cifar10_randomly_remove, arguments["num_remove"]
            ),
        },
        "fashion_mnist": {
            "iid": load_fashion_mnist_iid,
            "one_class_per_client": load_fashion_mnist_one_class_per_client,
            "sorted_partition": load_fashion_mnist_sorted_partition,
            "randomly_remove": partial(
                load_fashion_mnist_randomly_remove, arguments["num_remove"]
            ),
        },
    }
    arguments["load_function"] = load_functions[arguments["dataset"]][
        arguments["data_distribution"]
    ]

    # Client info is of the form i0.0[1],g0.0/2.0[1], etc.
    # Where the first character indicates the
    raw_clients = arguments["client_info"].split(",")

    arguments["client_runtimes"] = []
    for raw_client in raw_clients:
        # Get number of clients of this type.
        match = re.search(r"\[(\d+)\]", raw_client)
        if match:
            num_of_type = int(match.group(1))
        else:
            num_of_type = 1

        # Get client type and split out client parameters.
        client_runtime_constructor = CLIENT_TYPE_TO_MODEL[raw_client[0]]
        client_params = [
            float(x) for x in raw_client[1 : raw_client.index("[")].split("/")
        ]

        for _ in range(num_of_type):
            arguments["client_runtimes"].append(
                client_runtime_constructor(*client_params)
            )

    return arguments


def run_experiment(
    strategy: Strategy, args: Dict[str, Any], model_info: Tuple[str, callable]
):
    # Start a new wandb run to track this script
    model_name, model_generator = model_info
    run = wandb.init(
        # set the wandb project where this run will be logged
        project="afl-bench",
        entity="afl-bench",
        name=f"{strategy.name} {args['dataset']} {args['data_distribution'] + ((' ' + str(args['num_remove'])) if args['num_remove'] is not None else '')}, client info {args['client_info']}, buffer size {args['buffer_size']}",
        # track hyperparameters and run metadata
        config={
            "architecture": model_name,
            "dataset": args["dataset"],
            "data_distribution": args["data_distribution"],
            "num_remove": args["num_remove"],
            "wait_for_full": args["wait_for_full"],
            "buffer_size": args["buffer_size"],
            "ms_to_wait": args["ms_to_wait"],
            "num_clients": len(args["client_runtimes"]),
            "client_runtimes": args["client_runtimes"],
            "client_lr": args["client_lr"],
            "client_num_steps": args["client_num_steps"],
            "num_aggregations": args["num_aggregations"],
            "batch_size": args["batch_size"],
            "device": DEVICE,
        },
    )

    trainloaders, testloaders, global_testloader = args["load_function"](
        run.config["num_clients"], batch_size=run.config["batch_size"]
    )

    #########################################################################################
    # NOTE: NOTHING BELOW THIS LINE SHOULD BE CHANGED.                                      #
    #########################################################################################

    # Define a server where global model is a SimpleCNN and strategy is the one defined above.
    server = Server(
        model_generator().to(run.config["device"]),
        strategy,
        run.config["num_aggregations"],
        global_testloader,
        device=run.config["device"],
    )

    # Create client threads with models. Note runtime is instant
    # (meaning no simulated training delay).
    client_threads = []
    for i, runtime_model in enumerate(args["client_runtimes"]):
        client = Client(
            model_generator().to(run.config["device"]),
            trainloaders[i],
            testloaders[i],
            num_steps=run.config["client_num_steps"],
            lr=run.config["client_lr"],
            device=run.config["device"],
        )
        client_thread = ClientThread(
            client, server, runtime_model=runtime_model, client_id=i
        )
        client_threads.append(client_thread)

    # Start up server and start up all client threads.
    server.run()
    for client_thread in client_threads:
        client_thread.run()

    # Kill client threads once server stops.
    server.join()
    for client_thread in client_threads:
        client_thread.stop()

    wandb.finish()
