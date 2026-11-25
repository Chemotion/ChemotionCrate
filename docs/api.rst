API reference
=============

The public surface is what :mod:`chemotion_crate` exports, plus :mod:`chemotion_crate.patch`.
Other modules are internal and may change in any release.

Export
------

.. autofunction:: chemotion_crate.export

.. autofunction:: chemotion_crate.export_model

Authors
-------

.. autoclass:: chemotion_crate.Author
   :members:

.. autoclass:: chemotion_crate.AuthorAffiliation
   :members:

Reading
-------

.. autofunction:: chemotion_crate.read

.. autoclass:: chemotion_crate.ElnCrate
   :members:

.. autoclass:: chemotion_crate.ElnDataset
   :members:

.. autoclass:: chemotion_crate.ElnCollection
   :members:

.. autoclass:: chemotion_crate.ElnFile
   :members:

Export model
------------

.. autoclass:: chemotion_crate.ExportModel
   :members:

Patching ontology terms
-----------------------

.. automodule:: chemotion_crate.patch
   :members: patch_eln_ontology, patch_metadata_dict
