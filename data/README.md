# Demo data

Supporting data files for the TSSC validated pattern's remediation demo:

```
data/
  osv/LW-DEMO-00*.json         Lightwell OSV advisories — each aliases one real
                               CVE and declares the `.rhlw-*` backport as the
                               `fixed` version
  sbom/*.cdx.json              One-component CycloneDX catalogs that make the
                               `.rhlw-*` versions "known" to RHTPA
```

These files are loaded into RHTPA by the `components/ops` component (the
`rhtpa-load-lightwell-data` Job) during the automated install, after RHTPA
is healthy. Without them, the demo still runs but there is no recommended
backport for the AI to suggest during remediation.

**NOTE**: the canonical copies live in `components/ops/files/data/` (Helm
can only package files inside the chart directory). The copies here are
for discoverability. If you edit a data file, update BOTH locations — or
better, edit `components/ops/files/data/` and copy here:

```sh
cp -r components/ops/files/data/* data/
```
