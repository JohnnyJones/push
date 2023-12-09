
import torch
from torch.utils.data import DataLoader
from tqdm import tqdm
from typing import *

from push.bayes.infer import Infer
from push.particle import Particle
from push.lib.utils import detach_to_cpu


# =============================================================================
# Helper
# =============================================================================

def mk_optim(params):
    """
    Returns Adam optimizer.
    
    Args:
        params: Model parameters.
    
    Returns:
        torch.optim.Adam: Adam optimizer.
    """
    return torch.optim.Adam(params, lr=1e-4, weight_decay=1e-2)


# =============================================================================
# Deep Ensemble Training
# =============================================================================

def _deep_ensemble_main(particle: Particle, dataloader: DataLoader, loss_fn: Callable, epochs: int) -> None:
    other_particles = list(filter(lambda x: x != particle.pid, particle.particle_ids()))
    # Training loop
    for e in tqdm(range(epochs)):
        losses = []
        for data, label in dataloader:
            loss = particle.step(loss_fn, data, label).wait()
            losses += [loss]
            for pid in other_particles:
                particle.send(pid, "ENSEMBLE_STEP", loss_fn, data, label)
        # print(f"Average loss {particle.pid}", torch.mean(torch.tensor(losses)))
    # print(f"Average loss {particle.pid}", torch.mean(torch.tensor(losses)))


def _ensemble_step(particle: Particle, loss_fn: Callable, data, label, *args) -> None:
    particle.step(loss_fn, data, label, *args)


# =============================================================================
# Deep Ensemble Inference
# =============================================================================

def _leader_pred_dl(particle: Particle, dataloader: DataLoader, f_reg: bool = True, mode="mean") -> torch.Tensor:
    acc = []
    for data, label in dataloader:
        acc += [_leader_pred(particle, data, f_reg=f_reg, mode=mode)]
    return torch.cat(acc)


def _leader_pred(particle: Particle, data: torch.Tensor, f_reg: bool = True, mode="mean") -> torch.Tensor:
    other_particles = list(filter(lambda x: x != particle.pid, particle.particle_ids()))
    preds = []
    preds += [detach_to_cpu(particle.forward(data).wait())]
    for pid in other_particles:
        preds += [particle.send(pid, "ENSEMBLE_PRED", data).wait()]
    t_preds = torch.stack(preds, dim=1)
    if f_reg:
        if mode == "mean":
            return t_preds.mean(dim=1)
        elif mode == "median":
            return t_preds.median(dim=1).values
        elif mode == "min":
            return t_preds.min(dim=1).values
        elif mode == "max":
            return t_preds.max(dim=1).values
        else:
            raise ValueError(f"Mode {mode} not supported ...")
    else:
        cls = t_preds.softmax(dim=1).argmax(dim=1)
        return torch.mode(cls, dim=1)


def _ensemble_pred(particle: Particle, data) -> None:
    return detach_to_cpu(particle.forward(data).wait())


# =============================================================================
# Deep Ensemble
# =============================================================================

class Ensemble(Infer):
    """The Ensemble Class.
    Used for running deep ensembles.

    Args:
        mk_nn (Callable): The base model to be ensembled.
        *args (any): Any arguments required for base model to be initialized.
        num_devices (int, optional): The desired number of gpu devices that will be utilized. Defaults to 1.
        cache_size (int, optional): The size of cache used to store particles. Defaults to 4.
        view_size (int, optional): The number of particles to consider storing in cache. Defaults to 4.
    """
    def __init__(self, mk_nn: Callable, *args: any, num_devices: int = 1, cache_size: int = 4, view_size: int = 4) -> None:
        super(Ensemble, self).__init__(mk_nn, *args, num_devices=num_devices, cache_size=cache_size, view_size=view_size)
        
    def bayes_infer(self,
                    dataloader: DataLoader, epochs: int,
                    loss_fn=torch.nn.MSELoss(),
                    num_ensembles: int = 2, mk_optim=mk_optim,
                    ensemble_entry=_deep_ensemble_main, ensemble_state={}, f_save: bool = False):
        """
        Creates particles and launches push distribution training loop.

        Args:
            dataloader (Callable): Dataloader.
            epochs (int, optional): Number of epochs to train for.
            loss_fn (Callable): Loss function to be used during training.
            num_ensembles (int, optional): The number of models to be ensembled.
            mk_optim (any): Returns an optimizer.
            ensemble_entry (function): Training loop for deep ensemble.
            ensemble_state (dict): A dictionary to store state variables for ensembled models.
                                   For example, in SWAG, we need to know how many SWAG epochs have passed
                                   to properly calculate a running average of model weights.
            f_save (bool): Flag to save each particle/model. Requires "particles" folder in the root directory
                           of the script calling train_deep_ensemble.

        Returns:
            None
        """
        # 1. Create particles
        pids = [
            self.push_dist.p_create(mk_optim, device=(0 % self.num_devices), receive={
                "ENSEMBLE_MAIN": ensemble_entry,
                "LEADER_PRED_DL": _leader_pred_dl,
                "LEADER_PRED": _leader_pred,
            }, state=ensemble_state)]
        for n in range(1, num_ensembles):
            pids += [self.push_dist.p_create(mk_optim, device=(n % self.num_devices), receive={
                "ENSEMBLE_STEP": _ensemble_step,
                "ENSEMBLE_PRED": _ensemble_pred,
            }, state={})]

        # 2. Perform independent training
        self.push_dist.p_wait([self.push_dist.p_launch(0, "ENSEMBLE_MAIN", dataloader, loss_fn, epochs)])

        if f_save:
            self.push_dist.save()

    def posterior_pred(self, data: DataLoader, f_reg=True, mode="mean") -> torch.Tensor:
        if isinstance(data, torch.Tensor):
            fut = self.push_dist.p_launch(0, "LEADER_PRED", data, f_reg, mode)
            return self.push_dist.p_wait([fut])[fut._fid]
        elif isinstance(data, DataLoader):
            fut = self.push_dist.p_launch(0, "LEADER_PRED_DL", data, f_reg, mode)
            return self.push_dist.p_wait([fut])[fut._fid]
        else:
            raise ValueError(f"Data of type {type(data)} not supported ...")


# =============================================================================
# Deep Ensemble Training
# =============================================================================

def train_deep_ensemble(dataloader: Callable, loss_fn: Callable, epochs: int,
                        nn: Callable, *args, num_devices: int = 1, cache_size: int = 4, view_size: int = 4,
                        num_ensembles: int = 2, mk_optim = mk_optim,
                        ensemble_entry = _deep_ensemble_main, ensemble_state={}) -> List[torch.Tensor]:
    """Train a deep ensemble PusH distribution and return a list of particle parameters.

    Args:
        dataloader (Callable): Dataloader.
        loss_fn (Callable): Loss function to be used during training.
        epochs (int, optional): Number of epochs to train for.
        nn (Callable): The base model to be ensembled and trained.
        *args (any): Any arguments needed for the model's initialization.
        num_devices (int, optional): The desired number of gpu devices to be utilized during training. Defaults to 1.
        cache_size (int, optional): The desired size of cache allocated to storing particles. Defaults to 4.
        view_size (int, optional): The number of other particle's parameters that can be seen by a particle on a single GPU. Defaults to 4.
        num_ensembles (int, optional): The number of models to be ensembled. Defaults to 2.
        mk_optim (any, optional): Returns an optimizer. Defaults to mk_optim.
        ensemble_entry (function, optional): Training loop for deep ensemble. Defaults to _deep_ensemble_main.
        ensemble_state (dict, optional): a dictionary to store state variables for ensembled models. i.e. in swag we need to know how
           how many swag epochs have passed to properly calculate a running average of model weights. Defaults to {}.
    Returns:
        List[torch.Tensor]: Returns a list of all particle's parameters.
    """
    ensemble = Ensemble(nn, *args, num_devices=num_devices, cache_size=cache_size, view_size=view_size)
    ensemble.bayes_infer(dataloader, epochs, loss_fn=loss_fn, num_ensembles=num_ensembles, mk_optim=mk_optim,
                         ensemble_entry=ensemble_entry, ensemble_state=ensemble_state)
    return ensemble.p_parameters()
