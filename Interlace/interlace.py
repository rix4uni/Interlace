#!/usr/bin/python3

import sys
from sys import argv

from Interlace.lib.core.input import InputParser, InputHelper
from Interlace.lib.core.output import OutputHelper, Level
from Interlace.lib.threader import Pool


def task_queue_generator_func(arguments, output, repeat):
    tasks_data = InputHelper.process_data_for_tasks_iterator(arguments)
    tasks_count = tasks_data["tasks_count"]
    yield tasks_count
    tasks_generator_func = InputHelper.make_tasks_generator_func(tasks_data)
    for i in range(repeat):
        tasks_iterator = tasks_generator_func()
        for task in tasks_iterator:
            # output.terminal(Level.THREAD, task.name(), "Added to Queue")
            yield task


def main():
    parser = InputParser()
    arguments = parser.parse(argv[1:])
    output = OutputHelper(arguments)

    output.print_banner()

    if arguments.repeat:
        repeat = int(arguments.repeat)
    else:
        repeat = 1

    try:
        pool = Pool(
            arguments.threads,
            task_queue_generator_func(arguments, output, repeat),
            arguments.timeout,
            output,
            arguments.sober,
            silent=arguments.silent,
            output_helper=output,
            resume_file=arguments.resume
        )
        pool.run()
        
        # Clear resume file on successful completion
        if arguments.resume and pool.resume_manager:
            pool.resume_manager.clear()
            if not arguments.silent:
                output.terminal(Level.THREAD, "Resume", "All tasks completed. Resume file cleared.")
    except Exception as e:
        # Print clean error message without traceback for user-facing errors
        print(str(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
