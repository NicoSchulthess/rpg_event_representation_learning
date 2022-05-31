import argparse
from os.path import dirname
import torch
import torchvision
import os
import numpy as np
import tqdm

from utils.models import Classifier
from torch.utils.tensorboard import SummaryWriter
from utils.loader import Loader
from utils.loss import cross_entropy_loss_and_accuracy
from utils.dataset import NCaltech101


torch.manual_seed(1)
np.random.seed(1)


def FLAGS():
    parser = argparse.ArgumentParser("""Train classifier using a learnt quantization layer.""")

    # training / validation dataset
    parser.add_argument("--validation_dataset", default="", required=True)
    parser.add_argument("--training_dataset", default="", required=True)
    parser.add_argument("--testing_dataset", default="", required=True)

    # logging options
    parser.add_argument("--log_dir", default="", required=True)

    # loader and device options
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--num_workers", type=int, default=4)
    parser.add_argument("--pin_memory", type=bool, default=True)
    parser.add_argument("--batch_size", type=int, default=4)
    parser.add_argument("--lr", type=float, default=1e-4)

    parser.add_argument("--num_epochs", type=int, default=30)
    parser.add_argument("--save_every_n_epochs", type=int, default=5)
    
    parser.add_argument("--continue_from", default=None)

    parser.add_argument("--use_hp", default=False)

    flags = parser.parse_args()

    os.makedirs(flags.log_dir, exist_ok=True)
    assert os.path.isdir(dirname(flags.log_dir)), f"Log directory root {dirname(flags.log_dir)} not found."
    assert os.path.isdir(flags.validation_dataset), f"Validation dataset directory {flags.validation_dataset} not found."
    assert os.path.isdir(flags.training_dataset), f"Training dataset directory {flags.training_dataset} not found."
    assert os.path.isdir(flags.testing_dataset), f"Testing dataset directory {flags.testing_dataset} not found."

    print(f"----------------------------\n"
          f"Starting training with \n"
          f"num_epochs: {flags.num_epochs}\n"
          f"batch_size: {flags.batch_size}\n"
          f"lr: {flags.lr}\n"
          f"device: {flags.device}\n"
          f"use half precision: {flags.use_hp}\n"
          f"log_dir: {flags.log_dir}\n"
          f"training_dataset: {flags.training_dataset}\n"
          f"validation_dataset: {flags.validation_dataset}\n"
          f"testing_dataset: {flags.testing_dataset}\n"
          f"----------------------------")

    return flags

def percentile(t, q):
    B, C, H, W = t.shape
    k = 1 + round(.01 * float(q) * (C * H * W - 1))
    result = t.view(B, -1).kthvalue(k).values
    return result[:,None,None,None]

def create_image(representation):
    B, C, H, W = representation.shape
    representation = representation.view(B, 3, C // 3, H, W).sum(2)

    # do robust min max norm
    representation = representation.detach().cpu().float()
    robust_max_vals = percentile(representation, 99)
    robust_min_vals = percentile(representation, 1)

    representation = (representation - robust_min_vals)/(robust_max_vals - robust_min_vals)
    representation = torch.clamp(255*representation, 0, 255).byte()

    representation = torchvision.utils.make_grid(representation)

    return representation


if __name__ == '__main__':
    flags = FLAGS()

    import random
    random.seed(0)
    torch.manual_seed(0)
    np.random.seed(0)

    # datasets, add augmentation to training set
    training_dataset = NCaltech101(flags.training_dataset, augmentation=True)
    validation_dataset = NCaltech101(flags.validation_dataset, classes=training_dataset.classes)
    testing_dataset = NCaltech101(flags.testing_dataset, classes=training_dataset.classes)

    # construct loader, handles data streaming to gpu
    training_loader = Loader(training_dataset, flags, device=flags.device)
    validation_loader = Loader(validation_dataset, flags, device=flags.device)
    testing_loader = Loader(testing_dataset, flags, device=flags.device)

    # model, and put to device
    model = Classifier(num_classes=len(training_dataset.classes))
    
    if flags.continue_from is not None:
        assert os.path.exists(flags.continue_from), f"Checkpoint {flags.continue_from} not found"
        
        ckpt = torch.load(flags.continue_from, map_location=flags.device)
        model.load_state_dict(ckpt['state_dict'])
        iteration = ckpt['iteration']
        min_validation_loss = ckpt['min_val_loss']
        start_epoch = round(iteration / len(training_loader))

    else:
        iteration = 0
        min_validation_loss = np.inf
        start_epoch = 0
    
    model = model.to(flags.device)
    if flags.use_hp:
        model = model.half()

    # optimizer and lr scheduler
    eps = 1e-4 if flags.use_hp else 1e-8
    optimizer = torch.optim.Adam(model.parameters(), lr=flags.lr, eps=eps)
    lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, 0.5)

    os.makedirs(flags.log_dir, exist_ok=True)
    writer = SummaryWriter(flags.log_dir)


    for i in range(flags.num_epochs):
        if i < start_epoch:
            continue

        ######### Training #########
        sum_accuracy = 0
        sum_loss = 0

        model = model.train()
        print(f"Training step [{i:3d}/{flags.num_epochs:3d}]")
        for events, labels in tqdm.tqdm(training_loader):
            if events.size == 0:
                continue

            optimizer.zero_grad()

            pred_labels, representation = model(events, test=True, use_hp=flags.use_hp)
            loss, accuracy = cross_entropy_loss_and_accuracy(
                pred_labels, labels)

            loss.backward()

            optimizer.step()

            sum_accuracy += accuracy.detach()
            sum_loss += loss.detach()

            iteration += 1

            del events, labels, pred_labels, loss, accuracy

        if i % 10 == 9:
            lr_scheduler.step()

        training_loss = sum_loss.item() / len(training_loader)
        training_accuracy = sum_accuracy.item() / len(training_loader)
        print(f"Training Iteration {iteration:5d}  Loss {training_loss:.4f}  Accuracy {training_accuracy:.4f}")

        writer.add_scalar("training/accuracy", training_accuracy, iteration)
        writer.add_scalar("training/loss", training_loss, iteration)

        representation_vizualization = create_image(representation)
        writer.add_image("training/representation",
                         representation_vizualization, iteration)

        
        ######### Validation #########
        sum_accuracy = 0
        sum_loss = 0
        model = model.eval()

        print(f"Validation step [{i:3d}/{flags.num_epochs:3d}]")
        for events, labels in tqdm.tqdm(validation_loader):
            if events.size == 0:
                continue

            with torch.no_grad():
                pred_labels, representation = model(events, test=True, use_hp=flags.use_hp)
                loss, accuracy = cross_entropy_loss_and_accuracy(pred_labels, labels)

            sum_accuracy += accuracy.detach()
            sum_loss += loss.detach()

            del events, labels, pred_labels, loss, accuracy

        validation_loss = sum_loss.item() / len(validation_loader)
        validation_accuracy = sum_accuracy.item() / len(validation_loader)

        writer.add_scalar("validation/accuracy", validation_accuracy, iteration)
        writer.add_scalar("validation/loss", validation_loss, iteration)

        # visualize representation
        representation_vizualization = create_image(representation)
        writer.add_image("validation/representation", representation_vizualization, iteration)

        print(f"Validation Loss {validation_loss:.4f}  Accuracy {validation_accuracy:.4f}")

        if validation_loss < min_validation_loss:
            min_validation_loss = validation_loss
            state_dict = model.state_dict()

            torch.save({
                "state_dict": state_dict,
                "min_val_loss": min_validation_loss,
                "iteration": iteration
            }, os.path.join(flags.log_dir, "model_best.pth"))
            print("New best at ", validation_loss)

        if i % flags.save_every_n_epochs == 0:
            state_dict = model.state_dict()
            torch.save({
                "state_dict": state_dict,
                "min_val_loss": min_validation_loss,
                "iteration": iteration
            }, os.path.join(flags.log_dir, "checkpoint_%05d_%.4f.pth" % (iteration, min_validation_loss)))


        ######### Testing #########
        sum_accuracy = 0
        sum_loss = 0
        model = model.eval()

        print(f"Testing step [{i:3d}/{flags.num_epochs:3d}]")
        for events, labels in tqdm.tqdm(testing_loader):
            if events.size == 0:
                continue

            with torch.no_grad():
                pred_labels, representation = model(events, test=True, use_hp=flags.use_hp)
                loss, accuracy = cross_entropy_loss_and_accuracy(pred_labels, labels)

            sum_accuracy += accuracy.detach()
            sum_loss += loss.detach()

            del events, labels, pred_labels, loss, accuracy

        testing_loss = sum_loss.item() / len(testing_loader)
        testing_accuracy = sum_accuracy.item() / len(testing_loader)

        writer.add_scalar("testing/accuracy", testing_accuracy, iteration)
        writer.add_scalar("testing/loss", testing_loss, iteration)

        # visualize representation
        representation_vizualization = create_image(representation)
        writer.add_image("testing/representation", representation_vizualization, iteration)

        print(f"Testing Loss {testing_loss:.4f}  Accuracy {testing_accuracy:.4f}")
        writer.flush()
