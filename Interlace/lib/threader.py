import subprocess
import os
import queue
import platform
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from multiprocessing import Event, Lock
from tqdm import tqdm

from Interlace.lib.core.output import OutputHelper, Level

if platform.system().lower() == 'linux':
    shell = os.getenv("SHELL") if os.getenv("SHELL") else "/bin/sh"
else:
    shell = None


class Task(object):
    def __init__(self, command, silent=False):
        self.task = command
        self.self_lock = None
        self.sibling_locks = []
        self.silent = silent

    def __cmp__(self, other):
        return self.name() == other.name()

    def __hash__(self):
        return self.task.__hash__()

    def clone(self):
        new_task = Task(self.task, self.silent)
        new_task.self_lock = self.self_lock
        new_task.sibling_locks = self.sibling_locks
        return new_task

    def replace(self, old, new):
        self.task = self.task.replace(old, new)

    def run(self, t=False, timeout=None):
        for lock in self.sibling_locks:
            lock.wait()
        self._run_task(t, timeout)
        if self.self_lock:
            self.self_lock.set()

    def wait_for(self, siblings):
        for sibling in siblings:
            self.sibling_locks.append(sibling.get_lock())

    def name(self):
        return self.task

    def get_lock(self):
        if not self.self_lock:
            self.self_lock = Event()
            self.self_lock.clear()
        return self.self_lock

    def _run_task(self, t=False, timeout=None):
        if self.silent:
            s = subprocess.Popen(self.task, shell=True,
                                 stdout=subprocess.DEVNULL,
                                 encoding="utf-8",
                                 executable=shell)
            try:
                s.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                s.kill()
                s.communicate()
                raise
            return
        else:
            s = subprocess.Popen(self.task, shell=True,
                                 stdout=subprocess.PIPE,
                                 encoding="utf-8",
                                 executable=shell)
            try:
                out, _ = s.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                s.kill()
                s.communicate()
                raise

        if out != "":
            if t:
                t.write(out)
            else:
                print(out)


class Worker(object):
    def __init__(self, task_queue, timeout, output, tq, output_helper, resume_manager=None):
        self.queue = task_queue
        self.timeout = timeout
        self.output = output
        self.tqdm = tq
        self.output_helper = output_helper
        self.resume_manager = resume_manager

    def __call__(self):
        while True:
            try:
                task = self.queue.get(timeout=1)
            except queue.Empty:
                return

            if self.output_helper:
                self.output_helper.terminal(Level.THREAD, task.name(), "Added to Queue")

            try:
                if isinstance(self.tqdm, tqdm):
                    task.run(self.tqdm, timeout=self.timeout)
                    self.tqdm.update(1)
                else:
                    task.run(timeout=self.timeout)
                
                # Mark task as completed in resume file
                if self.resume_manager:
                    self.resume_manager.mark_completed(task)
            except subprocess.TimeoutExpired:
                if self.output_helper:
                    self.output_helper.terminal(Level.ERROR, task.name(), f"Task timed out after {self.timeout} seconds")
                if isinstance(self.tqdm, tqdm):
                    self.tqdm.update(1)
                # Mark as completed even on timeout (task was attempted)
                if self.resume_manager:
                    self.resume_manager.mark_completed(task)
            except Exception as e:
                if self.output_helper:
                    self.output_helper.terminal(Level.ERROR, task.name(), f"Task failed: {e}")
                if isinstance(self.tqdm, tqdm):
                    self.tqdm.update(1)
                # Mark as completed even on failure (task was attempted)
                if self.resume_manager:
                    self.resume_manager.mark_completed(task)


class ResumeManager(object):
    def __init__(self, resume_file):
        self.resume_file = resume_file
        self.completed_tasks = set()
        self.lock = Lock()
        self.load_completed_tasks()

    def load_completed_tasks(self):
        """Load completed tasks from resume file if it exists"""
        if os.path.exists(self.resume_file):
            try:
                with open(self.resume_file, 'r') as f:
                    data = json.load(f)
                    self.completed_tasks = set(data.get('completed_tasks', []))
            except (json.JSONDecodeError, IOError):
                # If file is corrupted, start fresh
                self.completed_tasks = set()

    def is_completed(self, task):
        """Check if a task has already been completed"""
        task_hash = self._hash_task(task)
        return task_hash in self.completed_tasks

    def mark_completed(self, task):
        """Mark a task as completed and save to file"""
        task_hash = self._hash_task(task)
        with self.lock:
            self.completed_tasks.add(task_hash)
            self._save_completed_tasks()

    def _hash_task(self, task):
        """Generate a hash for a task based on its command"""
        task_str = task.name() if hasattr(task, 'name') else str(task)
        return hashlib.md5(task_str.encode('utf-8')).hexdigest()

    def _save_completed_tasks(self):
        """Save completed tasks to resume file"""
        try:
            data = {
                'completed_tasks': list(self.completed_tasks),
                'total_completed': len(self.completed_tasks)
            }
            with open(self.resume_file, 'w') as f:
                json.dump(data, f, indent=2)
        except IOError:
            # If we can't write, continue without saving
            pass

    def clear(self):
        """Clear the resume file"""
        if os.path.exists(self.resume_file):
            try:
                os.remove(self.resume_file)
            except IOError:
                pass
        self.completed_tasks = set()


class Pool(object):
    def __init__(self, max_workers, task_queue, timeout, output, progress_bar, silent=False, output_helper=None, resume_file=None):
        max_workers = int(max_workers)
        tasks_count = next(task_queue)
        if not tasks_count:
            raise ValueError("The queue is empty")

        # Initialize resume manager if resume is enabled
        self.resume_manager = None
        completed_count = 0
        if resume_file:
            self.resume_manager = ResumeManager(resume_file)
            # Filter out already completed tasks
            filtered_tasks = []
            for task in task_queue:
                if not self.resume_manager.is_completed(task):
                    filtered_tasks.append(task)
                else:
                    completed_count += 1
            
            if completed_count > 0 and output_helper:
                output_helper.terminal(Level.THREAD, "Resume", f"Resuming: {completed_count} tasks already completed, {len(filtered_tasks)} remaining")
            
            task_queue = iter(filtered_tasks)
            tasks_count = len(filtered_tasks) + completed_count

        self.queue = queue.Queue()
        for task in task_queue:
            self.queue.put(task)

        self.timeout = timeout
        self.output = output
        self.max_workers = min(tasks_count, max_workers)

        self.output_helper = output_helper
        # Initialize progress bar with total including completed tasks
        if not progress_bar and not silent:
            self.tqdm = tqdm(total=tasks_count, initial=completed_count)
        else:
            self.tqdm = True

    def run(self):
        workers = [Worker(self.queue, self.timeout, self.output, self.tqdm, self.output_helper, self.resume_manager)
                   for _ in range(self.max_workers)]

        with ThreadPoolExecutor(self.max_workers) as executors:
            futures = [executors.submit(worker) for worker in workers]
            # Wait for all futures to complete
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    if self.output_helper:
                        self.output_helper.terminal(Level.ERROR, "Worker", f"Worker failed: {e}")


# test harness
if __name__ == "__main__":
    tasks = ["sleep 1",
             "sleep 2",
             "sleep 3",
             "sleep 4",
             "sleep 5",
             "sleep 6",
             "sleep 7",
             "sleep 8",
             "sleep 9",
             "sleep 1",
             "echo 'Char!'"]
    p = Pool(4, iter([len(tasks)] + [Task(t) for t in tasks]), 0, 0, True, output_helper=OutputHelper())
    p.run()
