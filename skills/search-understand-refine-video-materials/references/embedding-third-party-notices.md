# Offline embedding route third-party notices

The local vector route installs `intfloat/multilingual-e5-small` model files
from pinned revision `614241f622f53c4eeff9890bdc4f31cfecc418b3` into the
user's application-data directory. The upstream model card declares MIT
licensing: https://huggingface.co/intfloat/multilingual-e5-small.

The dedicated Python runtime installs checksum-locked distributions of
ONNX Runtime (MIT), Hugging Face Tokenizers (Apache-2.0), NumPy (BSD-3-Clause),
and their declared dependencies. Their full licenses remain available from
the installed distributions. No model weights are bundled into this Skill.
