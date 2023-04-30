from configs.config_utilities import load_config
from torch.utils.data import DataLoader, Subset

from models.pyramid import build_pyramid_occupancy_network, DefaultConfig
from dataset import NuScenesDataset
from logger import TensorboardLogger

import torch
import torch.nn as nn
import torch.optim as optim
import albumentations as A
from albumentations.pytorch import ToTensorV2

import platform
import re
import time
from tqdm import tqdm


def main():
    config = load_config()

    sample_tokens = config.sample_tokens  # get all tokens of scene-0061
    split_index = int(len(sample_tokens) // (1 / 0.7))

    train_transform = A.Compose(
        [
            A.HorizontalFlip(p=0.5),
            A.Rotate(limit=20, p=0.3),
            A.Normalize(
                mean=[0.485, 0.456, 0.406],
                std=[0.229, 0.224, 0.225],
                max_pixel_value=255.0,
            ),
        ]
    )

    train_dataset = NuScenesDataset(
        nuscenes_dir=config.nuscenes_dir,
        nuscenes_version=config.nuscenes_version,
        label_dir=config.label_dir,
        sample_tokens=sample_tokens[:split_index],
        # scene_names=config.train_scenes,
        image_size=(200, 196),
        flatten_labels=(config.method_type == "multiclass"),
        image_transform=train_transform,
    )

    train_loader = DataLoader(
        train_dataset,
        batch_size=config.batch_size,
        num_workers=config.num_workers,
        pin_memory=True,
        shuffle=True,
    )
    validate_dataset = NuScenesDataset(
        nuscenes_dir=config.nuscenes_dir,
        nuscenes_version=config.nuscenes_version,
        label_dir=config.label_dir,
        sample_tokens=sample_tokens[split_index:],
        # scene_names=config.val_scenes,
        image_size=(200, 196),
        flatten_labels=(config.method_type == "multiclass"),
    )

    validate_loader = DataLoader(
        validate_dataset,
        batch_size=config.batch_size,
        num_workers=config.num_workers,
        pin_memory=True,
        shuffle=True,
    )

    this_device = platform.platform()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        device = "cuda"
    elif re.search("arm64", this_device):
        # use Apple GPU
        device = "mps"
    else:
        device = "cpu"

    network = build_pyramid_occupancy_network().to(device)

    if train_dataset.flatten_labels:
        criterion = nn.CrossEntropyLoss().to(device)  # multiclass
        num_class = 15
        task = "multiclass"
    else:
        criterion = nn.BCEWithLogitsLoss().to(device)  # multilabel
        num_class = 14
        task = "multilabel"

    optimizer = optim.Adam(network.parameters(), lr=config.lr)

    current_time = time.time()

    logger = TensorboardLogger(
        device,
        log_dir=f"{config.log_dir}/PON_{task}_{current_time}",
        validate_loader=validate_loader,
        criterion=criterion,
        n_classes=num_class,
        task=task,
    )

    config_log_table = f"""
        <table>
            <tr>
                <th>Nuscenes Version</th>
                <th>Is augmentation</th>
                <th>Batch Size</th>
                <th>Num Workers</th>
                <th>Learning Rate</th>
                <th>Number of epochs</th>
                <th>Device</th>
                <th>Loss function</th>
                <th>Optimizer</th>
            </tr>
            <tr>
                <td>{config.nuscenes_version}</td>
                <td>{train_dataset.image_transform is not None}</td>
                <td>{config.batch_size}</td>
                <td>{config.num_workers}</td>
                <td>{config.lr}</td>
                <td>{config.epochs}</td>
                <td>{device}</td>
                <td>{criterion.__class__.__name__}</td>
                <td>{optimizer.__class__.__name__}</td>
            </tr>
        </table>
    """

    logger.writer.add_text("Experiment Configurations", config_log_table, global_step=0)

    for epoch in tqdm(range(config.epochs)):
        for batch_idx, batch in enumerate(train_loader):
            images, labels, masks, calibs = batch
            images = images.to(device)
            labels = labels.to(device)
            masks = masks.to(device)
            calibs = calibs.to(device)

            predictions = network(images, calibs)

            masks_to_ignore = (masks == -1).long()  # makes mask (-2, -1) to (0, 1)
            masks_to_ignore = masks_to_ignore.unsqueeze(1).repeat(1, 14, 1, 1)

            # compute loss
            if criterion.__class__.__name__ == "CrossEntropyLoss":
                loss = criterion(predictions, labels.long()).to(device)
            else:
                loss = criterion(predictions, labels.float()).to(device)

            # compute gradient
            optimizer.zero_grad()
            loss.backward()

            # update weights
            optimizer.step()

            logger.log_step(loss=loss.item())

        logger.log_epoch(network, epoch)


if __name__ == "__main__":
    main()
