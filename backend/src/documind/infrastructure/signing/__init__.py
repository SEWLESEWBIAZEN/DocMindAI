"""DocuMind Sign PDF engine (design §6, §7.3, §9.2).

Pure byte-in, byte-out transforms with no database or storage access:

- :mod:`.snapshot`: inspect, clean and flatten the source PDF into the signing snapshot;
- :mod:`.images`: clean uploaded or drawn signature images and render typed signatures;
- :mod:`.stamp`: draw completed fields onto the snapshot;
- :mod:`.certificate`: render the completion certificate;
- :mod:`.fonts`: the bundled fonts and per-character font fallback.
"""
