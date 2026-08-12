# License review required before publication

No `LICENSE` file is included yet. Without one, the repository is source-visible
but not open source and no broad reuse permission is granted.

The license decision is blocked on three reviews:

1. The direct Telegram library dependency is GPL-2.0-only. The combined Python
   distribution model must be reviewed before selecting MIT, Apache-2.0, GPL,
   or another project license.
2. The dashboard implementation evolved from a visual design prototype. The
   right to relicense every derived UI element must be confirmed even though
   reference code, screenshots, fonts, and media are excluded here.
3. WHOOP API and brand terms must be reviewed, including any approval required
   for a public integration or public statements about interoperability.

Possible paths, requiring explicit owner/legal approval:

- replace or isolate the GPL dependency, complete UI provenance review, then
  consider a permissive license;
- retain the dependency and adopt a reviewed GPL-compatible distribution model;
- publish only after written provider/brand approval where required.

This file is an engineering release gate, not legal advice.
