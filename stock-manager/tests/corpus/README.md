# Reading-accuracy corpus

Each case is two files with the same name:

    name.jpg     the photo of the document
    name.json    {"type": "note"|"list", "expected": { ... }}

`expected` holds only the fields that must be checked: a case can cover the header alone, or the
header and every line. Lines are compared in order. A field may accept several readings, because
the model is not deterministic:

    "supplier": {"$or": ["ACME", "ACME BUILDING SUPPLIES"]}

Run it with `.venv/bin/python -m inventory.eval`. It calls the real model once per case.

Generate the photo for the case shipped here:

    python tests/fixtures/make_sample.py && cp tests/fixtures/bon_sample.jpg tests/corpus/sample_note.jpg

**Grow it with real documents.** The case shipped here is a generated note, useful to check that
the mechanism works, but it says nothing about accuracy on your suppliers' real layouts. Aim for
30 to 50 documents covering every regular supplier, every trap already met (a RAL colour mistaken
for a backorder, "ditto" lines, a second sheet visible behind the first, a balance column), and a
a few bad photos on purpose (at an angle, dark, folded).

Photos in this folder are company documents: `.gitignore` keeps `*.jpg` out of the repository, and
they must not be published.
