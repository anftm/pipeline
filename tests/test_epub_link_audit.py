import unittest

from scripts.audit_epub_links import body_text, inspect_documents


class LinkAuditTests(unittest.TestCase):
    def test_distinguishes_missing_paths_anchors_and_non_document_links(self):
        documents = {
            "text/one.xhtml": '<html:body id="root"><html:a href="../notes/two%20words.xhtml#%E6%B3%A8%E9%87%8A">note</html:a><a href="#root">top</a><a href="../notes/two%20words.xhtml#absent">bad anchor</a><a href="missing.xhtml">bad file</a><a href="../cover.png">image</a><a href="https://example.org/">web</a></html:body>',
            "notes/two words.xhtml": '<p id="注释">note</p>',
        }
        result = inspect_documents(documents, set(documents) | {"cover.png"})
        self.assertEqual(result["counts"], {"valid_internal": 2, "missing_anchor": 1,
                                           "missing_document": 1, "attachment": 1, "external": 1})
        self.assertEqual([p["reason"] for p in result["problems"]], ["missing_anchor", "missing_document"])

    def test_body_text_decodes_entities_and_ignores_active_content(self):
        source = '<?xml version="1.0" encoding="utf-8"?><html><head><title>title</title></head><body>正文&nbsp;甲<!-- hidden comment --><script>bad</script>乙</body></html>'
        generated = '<html xmlns="http://www.w3.org/1999/xhtml"><body>正文&#160;甲乙</body></html>'
        self.assertEqual(body_text(source), '正文甲乙')
        self.assertEqual(body_text(source), body_text(generated))
