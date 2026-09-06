Python API
==========

Programs written in Python can read the current execution context without importing Atlas's host
implementation:

.. code-block:: python

   from atlas_core import get_context

   context = get_context()
   print(context.host.id)
   print(context.program.root)
   print(context.command.name)
   print(context.execution.operation_id)

The same context is available to non-Python programs as the JSON document named by
``ATLAS_CONTEXT_FILE``. The public package provides host identity, standard paths, execution context,
volatile execution storage, and external secret retrieval.

.. automodule:: atlas_core
   :members:
   :undoc-members:

.. automodule:: atlas_core.context
   :members:
   :undoc-members:

.. automodule:: atlas_core.host
   :members:
   :undoc-members:

.. automodule:: atlas_core.paths
   :members:
   :undoc-members:

Secrets
-------

.. automodule:: atlas_core.secrets
   :members:

Execution storage
-----------------

.. automodule:: atlas_core.execution
   :members:
