"""Executor adapters loaded by ``bin/executor-run``.

Each adapter module translates the unified executor contract to one CLI:

* ``build_command(ctx)`` -> ``list[str]``
* ``translate_result(stdout, stderr, raw_exit)`` -> unified result dict

Adapters must stay pure translators: no Hermes imports, no ``kanban.db``
access, no task/business state, and no logging of the task book.
"""
