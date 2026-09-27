# Third-party notices for optional local Top-K assets

The v0.4 offline vector route installs `intfloat/multilingual-e5-small` from a
fixed Hugging Face revision into the user's local application-data directory.
The model card declares the model under the MIT license. The model files are
not included in this repository or the CLI wheels. The installer verifies
SHA-256 checksums before use.

- Model: https://huggingface.co/intfloat/multilingual-e5-small
- Revision: `614241f622f53c4eeff9890bdc4f31cfecc418b3`
- ONNX Runtime: https://github.com/microsoft/onnxruntime (MIT)
- Hugging Face Tokenizers: https://github.com/huggingface/tokenizers (Apache-2.0)
- NumPy: https://github.com/numpy/numpy (BSD-3-Clause)

The repository's own MIT `LICENSE` remains unchanged. Consult the upstream
packages and model card for their full license and attribution terms.
