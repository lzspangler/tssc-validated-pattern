# ops

Operational jobs that run after the platform is up:

* **`rhtpa-load-lightwell-data` Job** — uploads the Lightwell OSV advisories
  and catalog SBOMs into RHTPA, making the `.rhlw-*` backports known and
  `fixed`. Without this the demo runs but the AI has no recommended backport.
* **`ai-agent-image-prewarm` DaemonSet** — pre-pulls the ~1.8 GB agent image
  onto every node so Tekton steps never hit a transient CDN failure.
* **`rhtpa-upload-advisory` ConfigMap** — ad-hoc upload script for pushing
  individual documents to RHTPA by hand.

The Lightwell data files live in `files/data/` (Helm can only package files
inside the chart directory — this is the canonical location, not a copy).
