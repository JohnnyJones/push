from multiprocessing import Queue
import torch
import torch.multiprocessing as mp
from typing import *

from push.lib.node_event_loop import NodeEventLoop
from push.lib.messages import *
from push.lib.waitable import Waitable

from push.pfuture import PFuture

    

def init_node_event_loop(mk_module: Callable,
                         args: list[any],
                         in_queues: dict[int, Queue],
                         out_queues: dict[int, Queue],
                         rank: int,
                         devices: list[int],
                         cache_size: int,
                         view_size: int) -> None:
    nel = NodeEventLoop(mk_module, args, in_queues[rank], out_queues[rank], rank, devices, cache_size, view_size)
    out_queues[rank].put(DevEvtLoopInitMSG())
    nel._start_event_loop()


class PusH(Waitable):
    """PusH Distribution.

    1. Create a Push Distribution which approximates a distribution on nn's parameters via *particles*.
    2. Create arbitrary number of particles (pinit). Particles execute concurrently of other particles
    """    
    def __init__(self, mk_module: Callable, *args, cache_size=4, view_size=4) -> None:
        # Model
        self.mk_module = mk_module
        self.args = args

        # Process management
        try:
            mp.set_start_method("spawn")
        except:
            pass
        self._manager = mp.Manager()
        
        # Message queues for device event loops
        self._in_queues = {}             # device -> queue
        self._out_queues = {}            # device -> queue
        self._processes = {}             # device -> process
        self._particle_to_device = {}    # pid -> device
        self._particle_to_rank = {}      # pid -> rank
        
        # Device manager
        self.cache_size = cache_size
        self.view_size = view_size
        self.rank = 0
        self._init()

        # Tasks and results
        self._future_id = 0
        self._particle_to_futures = {-1: []}
        self._future_to_particle = {}
        self._results = {}

    def _init(self) -> None:
        # Create mailbox
        devices = []
        for device_id in range(torch.cuda.device_count()):
            devices += [device_id]
        
        self._in_queues[self.rank] = self._manager.Queue()
        self._out_queues[self.rank] = self._manager.Queue()

        # Start device event loops
        p = mp.Process(
            target=init_node_event_loop,
            args=(
                self.mk_module,
                self.args,
                self._in_queues,
                self._out_queues,
                self.rank,
                devices,
                self.cache_size,
                self.view_size,
            ))
        self._processes[self.rank] = p
        p.start()

        # Acknowledge that device event loops have been started
        msg = self._out_queues[self.rank].get()
        if not isinstance(msg, DevEvtLoopInitMSG):
            raise ValueError(f"Fatal error ... inconsistent message state {msg}")

    # -----------------------------------------------------
    # Context management
    # -----------------------------------------------------

    def __enter__(self):
        return self

    def _cleanup(self) -> None:
        for device_id, proc in self._processes.items():
            self._in_queues[device_id].put(DevEvtLoopCleanupMSG())
        for device_id, proc in self._processes.items():
            proc.join()

    def __exit__(self, exc_type, exc_value, traceback):
        self._cleanup()

    # -----------------------------------------------------
    # Helper functionality
    # -----------------------------------------------------

    def _create_future_id(self):
        fid = self._future_id
        self._future_id += 1
        return fid

    def _register_future(self, pid: int, fid: int) -> None:
        self._particle_to_futures[pid] += [fid]
        self._future_to_particle[fid] = pid

    def _pwait(self, fids: list[int]) -> dict[int, any]:
        remaining = set(fids)
        acc = {}
        def loop():
            for fid in fids:
                if fid in self._results:
                    y = self._results.pop(fid)
                    acc[fid] = y
                    remaining.remove(fid)
                    pid = self._future_to_particle.pop(fid)
                    self._particle_to_futures[pid].remove(fid)
        loop()

        while len(remaining) > 0:
            for fid in fids:
                if fid in remaining:
                    pid = self._future_to_particle[fid]
                    rank = self._particle_to_rank[pid]
                    msg = self._out_queues[rank].get()

                    if isinstance(msg, Exception):
                        raise msg
                    elif isinstance(msg, ReceiveFuncAckPNNMSG):
                        self._results[msg.pid_fid[1]] = None
                    loop()
                    break
        return acc

    def _wait(self, fid: int) -> any:
        raise NotImplementedError
    

    # -----------------------------------------------------
    # Particles API
    # -----------------------------------------------------

    def particle_ids(self) -> List[int]:
        """Returns all particles.

        Returns:
            List[int]: List of all particle identifiers visible to current particle.
        """
        return list(self._particle_to_rank.keys())

    def num_particles(self) -> int:
        """Returns number of particles.

        Returns:
            int: The number of particles.
        """        
        return len(self._particle_to_rank)

    def p_create(self, mk_optim: Callable, device=0, receive={}, state={}) -> int:
        """Create a particle

        Args:
            mk_optim (Callable): Optimizer for updating parameters. Can be None.
            device (int, optional): Device to put particle on. Defaults to 0.
            receive (dict, optional): 
               Dictionary containing messages that this particle will respond to.
               Defaults to {}.

        Returns:
            int: Particle identifier.
        """        
        # Create particle
        new_pid = len(self._particle_to_rank)
        self._particle_to_device[new_pid] = device
        self._particle_to_rank[new_pid] = self.rank
        self._particle_to_futures[new_pid] = []
        self._in_queues[self.rank].put(ReceiveParticleInitPNNMSG(device, new_pid, mk_optim, receive, state))
        
        # Acknowledge
        msg = self._out_queues[self.rank].get()
        if not isinstance(msg, ReceiveParticleInitAckPNNMSG):
            raise ValueError(f"Fatal error ... inconsistent message state")

        # Broadcast so we can discover other particles
        for pid, queue in self._in_queues.items():
            queue.put(DELBroadcastParticlesMSG(self._in_queues, self._out_queues, self._particle_to_device))
            msg = self._out_queues[pid].get()
            if not isinstance(msg, DELBroadcastParticlesAckMSG):
                raise ValueError(f"Fatal error ... inconsistent message state")
        
        return new_pid

    def p_parameters(self, pid: int, sync=True) -> Union[int, Iterable[torch.Tensor]]:
        """_summary_

        Args:
            pid (int): Identifier of particle to obtain parameters of.
            sync (bool, optional): Obtain parameters. Defaults to True.

        Returns:
            Union[int, Iterable[torch.Tensor]]: Parameters
        """        
        if pid not in self._particle_to_rank:
            raise ValueError(f"Particle {pid} does not exist")

        # Create task
        fid = self._create_future_id()
        self._register_future(pid, fid)

        # Initiate task
        rank = self._particle_to_rank[pid]
        self._in_queues[rank].put(ReceiveParametersPNNMSG((pid, fid), pid))

        if sync:
            # Synchronize
            y = self._out_queues[rank].get()
            self._results[fid] = y
            while y.pid_fid[1] != fid:
                y = self._out_queues[rank].get()
                self._results[fid] = y
            return y.params
        else:
            # return fid
            return PFuture(self, -1, pid, fid)

    def p_launch(self, pid_to: int, msg: str, *args, sync=False) -> PFuture:
        """Launch a particle.

        Args:
            pid_to (int): Identifier of particle that is the main entry point.
            msg (str): Message associated with main function
            sync (bool, optional): Async. Defaults to False.

        Returns:
            PFuture: Calling wait will get the result of the computation.
        """        
        if pid_to not in self._particle_to_rank:
            raise ValueError(f"Particle {pid_to} does not exist")
        
        # Create task
        fid = self._create_future_id()
        self._register_future(pid_to, fid)

        # Send message
        rank = self._particle_to_rank[pid_to]
        self._in_queues[rank].put(ReceiveFuncPNNMSG((pid_to, fid), pid_to, msg, args))

        if sync:
            # Synchronize
            y = self._out_queues[rank].get()
            self._results[fid] = y
            while y.pid_fid[1] != fid:
                y = self._out_queues[rank].get()
                self._results[fid] = y
            return None
        else:
            # return fid
            return PFuture(self, -1, pid_to, fid)

    def p_wait(self, futures: list[PFuture]) -> dict[int, any]:
        return self._pwait([future._fid for future in futures])

    # -----------------------------------------------------
    # Utility
    # -----------------------------------------------------

    def save(self, sync=True):
        pid = 0

        # Create task
        fid = self._create_future_id()
        self._register_future(pid, fid)

        # Initiate task
        rank = self._particle_to_rank[pid]
        self._in_queues[rank].put(NELSaveModel((pid, fid)))

        if sync:
            # Synchronize
            y = self._out_queues[rank].get()
            self._results[fid] = y
            while y.pid_fid[1] != fid:
                y = self._out_queues[rank].get()
                self._results[fid] = y
            return None
        else:
            # return fid
            return PFuture(self, -1, pid, fid)