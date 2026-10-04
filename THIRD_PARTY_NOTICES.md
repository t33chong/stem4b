# Third-party dependencies

The MIT license covers stem4b's own code. Dependencies, model services and input
documents have their own terms; MIT does not replace those terms.

- **PyMuPDF / MuPDF**: available under AGPL or commercial licensing. Review
  [PyMuPDF's licensing information](https://pymupdf.io/licensing) before distributing
  software or images containing it or offering a service that uses it.
- **FFmpeg**: licensing depends on the build and enabled components. The Docker
  image installs Debian's FFmpeg package. See [FFmpeg's legal page](https://ffmpeg.org/legal.html)
  and the license/copyright files supplied with that package.
- Python dependencies are declared in `pyproject.toml`; installed distributions
  include their own license metadata. The Docker image also contains Python and
  Debian packages under their respective licenses.

No model weights are distributed. Check the terms of your chosen LLM and speech
providers and ensure you have the necessary rights to process or share your inputs
and generated audio. stem4b does not remove DRM.
