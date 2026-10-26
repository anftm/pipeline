"""Text contracts exercise real offsets/geometry, not recognition accuracy."""

import copy
import unittest

from scripts import ocr_layout, pdf_text_layer as layer


def raw_page(text="English", confidence=.99, mode="horizontal-ltr"):
    block = {"t": text, "b": [.1, .1, .8, .2], "c": confidence, "s": "ocr"}
    return {"version": 1, "kind": "pdf-ocr-page", "page": 1, "source": "ocr",
            "width": 100, "height": 200,
            **ocr_layout.arrange([block] if text else [], 100, 200, {"writing_mode": mode},
                                  include_writing_modes=True)}


class TextLayerTests(unittest.TestCase):
    def test_formula_relation_uses_exact_corrected_text_and_source_arrow_order(self):
        blocks=[{'t':'S-2+H2O=HS-1+OH-1','b':[.1,.1,.9,.2],'c':.8,'s':'ocr'},
                {'t':'1','b':[.3,.21,.4,.3],'c':.5,'s':'ocr'},
                {'t':'H+1+OH-1','b':[.2,.32,.6,.4],'c':.8,'s':'ocr'}]
        payload={**raw_page(),**ocr_layout.arrange(blocks,100,200,{'writing_mode':'horizontal-ltr'})}
        original=layer.from_page(payload,'a'*64,'ch')
        proposal=self.visual_proposal(original)
        proposal['replacements']=[{'region_id':'b0','before':blocks[0]['t'],'after':'S⁻² + H₂O ⇌ HS⁻¹ + OH⁻¹'},
                                  {'region_id':'b1','before':'1','after':'⇅'},
                                  {'region_id':'b2','before':blocks[2]['t'],'after':'H⁺¹ + OH⁻¹'}]
        anchor=lambda identity,value,start:{'region_id':identity,'text':value,'start':start,'end':start+len(value)}
        relation={'id':'f'+'1'*16,'kind':'printed-equilibrium','axis':'vertical','directions':['up','down'],
                  'box':[.3,.21,.4,.3],'quad':[[.3,.21],[.4,.21],[.4,.3],[.3,.3]],
                  'operator':anchor('b1','⇅',0),'from':anchor('b0','H₂O',6),
                  'to':anchor('b2','H⁺¹ + OH⁻¹',0),'evidence':{'crop_sha256':'e'*64,'reason':'Left arrow up, right arrow down'}}
        proposal['formula_relations']=[relation]
        accepted=layer.accept_proposal(original,proposal,actor='source-image-review')
        self.assertEqual(accepted['formula_relations'],[relation])
        self.assertEqual(accepted['raw_sha256'],original['raw_sha256'])
        self.assertEqual(original['regions'][0]['text'],blocks[0]['t'])
        proposal['formula_relations']=[{**relation,'directions':['down','up']}]
        with self.assertRaisesRegex(ValueError,'arrow order'):
            layer.accept_proposal(original,proposal,actor='reviewer')
        proposal['formula_relations']=[{**relation,'from':anchor('b0','H20',6)}]
        with self.assertRaisesRegex(ValueError,'anchor text'):
            layer.accept_proposal(original,proposal,actor='reviewer')

    def test_reversed_source_arrow_order_is_not_normalized_to_chemical_convention(self):
        blocks=[{'t':'H₂O','b':[.1,.1,.3,.2],'c':1,'s':'ocr'},
                {'t':'⇵','b':[.1,.2,.2,.3],'c':1,'s':'ocr'},
                {'t':'H⁺¹ + OH⁻¹','b':[.1,.3,.4,.4],'c':1,'s':'ocr'}]
        payload={**raw_page(),**ocr_layout.arrange(blocks,100,200,{'writing_mode':'horizontal-ltr'})}
        original=layer.from_page(payload,'a'*64,'ch');proposal=self.visual_proposal(original)
        def anchor(identity,value):return {'region_id':identity,'text':value,'start':0,'end':len(value)}
        proposal['formula_relations']=[{'id':'f'+'1'*16,'kind':'printed-equilibrium','axis':'vertical',
            'directions':['down','up'],'operator':anchor('b1','⇵'),'from':anchor('b0','H₂O'),
            'to':anchor('b2','H⁺¹ + OH⁻¹'),'box':[.1,.2,.2,.3],
            'quad':[[.1,.2],[.2,.2],[.2,.3],[.1,.3]],
            'evidence':{'crop_sha256':'d'*64,'reason':'Original reversed arrow pairing'}}]
        accepted=layer.accept_proposal(original,proposal,actor='reviewer')
        self.assertEqual(accepted['text'],original['text'])
        self.assertEqual(accepted['formula_relations'][0]['directions'],['down','up'])

    def visual_proposal(self,original):
        return {'kind':'pdf-text-correction','version':1,'page':original['page'],
                'base_generation':original['generation'],'raw_sha256':original['raw_sha256'],
                'page_identity':original['page_identity'],'replacements':[],
                'visual_review':{'kind':'source-image-review','source_sha256':original['source_sha256'],
                    'page':original['page'],'input_png_sha256':'c'*64,'basis':'Complete source map and enlarged glyphs'}}

    def test_visual_additions_and_nontext_exclusions_preserve_raw_and_source_geometry(self):
        blocks=[{'t':'MONGOLIAS','b':[.1,.1,.5,.2],'c':.99,'s':'ocr'},
                {'t':'D','b':[.7,.7,.8,.8],'c':.99,'s':'ocr'},
                {'t':'BÉNIN','b':[.1,.4,.4,.5],'c':.99,'s':'ocr'}]
        payload={**raw_page(),**ocr_layout.arrange(blocks,100,200,{'writing_mode':'horizontal-ltr'})}
        original=layer.from_page(payload,'a'*64,'en');before=copy.deepcopy(original)
        proposal=self.visual_proposal(original)
        proof={'crop_sha256':'d'*64,'reason':'Source reviewed printed glyph, not inferred from a country name'}
        proposal.update(replacements=[{'region_id':'b0','before':'MONGOLIAS','after':'MONGOLIA'}],
            exclusions=[{'region_id':'b1','before':'D','classification':'map-geometry','evidence':proof}],
            additions=[{'region_id':'v'+'1'*16,'text':'H','box':[.4,.3,.5,.4],
                'quad':[[.4,.3],[.5,.3],[.5,.4],[.4,.4]],'evidence':proof}],
            region_order=['b0','v'+'1'*16,'b2'])
        accepted=layer.accept_proposal(original,proposal,actor='explicit-source-review')
        self.assertEqual(accepted['text'],'MONGOLIA\nH\nBÉNIN')
        self.assertEqual(original,before)
        self.assertEqual(accepted['raw_sha256'],original['raw_sha256'])
        self.assertEqual(accepted['regions'][1]['confidence'],0)
        self.assertEqual(accepted['regions'][1]['recognition_status'],'source-reviewed-not-model-recognized')
        self.assertEqual(accepted['regions'][2]['quad'],next(r['quad'] for r in original['regions'] if r['id']=='b2'))
        audit=accepted['acceptances'][-1]
        self.assertEqual(audit['excluded_regions'][0]['region'],next(r for r in original['regions'] if r['id']=='b1'))
        self.assertEqual(accepted['quality'],'partially-reviewed')
        self.assertIn('BÉNIN',accepted['text'])

    def test_positioned_changes_need_source_review_and_exact_complete_order(self):
        original=layer.from_page(raw_page('real text'),'a'*64,'en')
        proposal=self.visual_proposal(original)
        proof={'crop_sha256':'d'*64,'reason':'Original source pixels'}
        proposal.update(additions=[{'region_id':'v'+'1'*16,'text':'P','box':[.1,.3,.2,.4],
            'quad':[[.1,.3],[.2,.3],[.2,.4],[.1,.4]],'evidence':proof}],region_order=['b0','v'+'1'*16])
        for change in [{'visual_review':{}},{'visual_review':{**proposal['visual_review'],'source_sha256':'b'*64}},
                       {'region_order':['b0']},{'region_order':['b0','b0']},
                       {'additions':[{**proposal['additions'][0],'evidence':{}}]}]:
            with self.subTest(change=change),self.assertRaises(ValueError):
                layer.accept_proposal(original,{**proposal,**change},actor='reviewer')

    def test_illustration_woodgrain_is_reviewed_geometry_not_unpositioned_text_deletion(self):
        blocks=[{'t':'Title','b':[.1,.1,.6,.2],'s':'ocr','c':1},
                {'t':'umms','b':[.1,.5,.4,.6],'s':'ocr','c':.4}]
        raw={**raw_page(),**ocr_layout.arrange(blocks,100,200,{'writing_mode':'horizontal-ltr'})}
        original=layer.from_page(raw,'a'*64,'ch');proposal=self.visual_proposal(original)
        proposal.update(exclusions=[{'region_id':'b1','before':'umms','classification':'illustration-geometry',
            'evidence':{'crop_sha256':'d'*64,'reason':'Original illustration woodgrain, not lettering'}}],region_order=['b0'])
        accepted=layer.accept_proposal(original,proposal,actor='source-image-review')
        self.assertEqual(accepted['text'],'Title')
        self.assertEqual(accepted['acceptances'][-1]['excluded_regions'][0]['region']['text'],'umms')
        self.assertEqual(original['text'],'Title\n\numms')

    def test_reviewed_handwritten_mark_is_kept_outside_body_not_erased_or_transcribed_as_nine(self):
        blocks=[{'t':'Printed paragraph.','b':[.1,.1,.8,.2],'s':'ocr','c':1},
                {'t':'9','b':[.02,.3,.06,.35],'s':'ocr','c':.4},
                {'t':'Second printed paragraph.','b':[.1,.4,.8,.5],'s':'ocr','c':1}]
        raw={**raw_page(),**ocr_layout.arrange(blocks,100,200,{'writing_mode':'horizontal-ltr'})}
        original=layer.from_page(raw,'a'*64,'en');proposal=self.visual_proposal(original)
        proposal.update(annotations=[{'region_id':'b1','before':'9','classification':'marginal-mark',
            'status':'unresolved','literal_text':None,'evidence':{'crop_sha256':'d'*64,
            'reason':'User original complete page shows pencil margin mark, not body digit'}}],region_order=['b0','b2'])
        accepted=layer.accept_proposal(original,proposal,actor='explicit-source-review')
        self.assertEqual(accepted['text'],'Printed paragraph.\nSecond printed paragraph.')
        self.assertEqual(original['regions'][1]['text'],'9')
        self.assertEqual(accepted['annotations'][0]['original_region'],original['regions'][1])
        self.assertIsNone(accepted['annotations'][0]['literal_text'])
        self.assertEqual(accepted['annotations'][0]['status'],'unresolved')
        self.assertEqual(accepted['raw_sha256'],original['raw_sha256'])
        self.assertIn('unresolved-source-annotation',accepted['review_flags'])
        changed=copy.deepcopy(proposal);changed['annotations'][0]['literal_text']='?'
        with self.assertRaises(ValueError):layer.accept_proposal(original,changed,actor='reviewer')
        for mutation in [
            {'annotations':proposal['annotations']*2},
            {'annotations':[{**proposal['annotations'][0],'before':'unmatched'}]},
            {'annotations':[{**proposal['annotations'][0],'evidence':{}}]},
            {'annotations':[{**proposal['annotations'][0],'classification':'scan-artifact'}]},
            {'visual_review':{}},
            {'region_order':['b0','b1','b2']}]:
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                layer.accept_proposal(original,{**proposal,**mutation},actor='reviewer')

    def test_positioned_map_additions_preserve_unchanged_body_separators(self):
        blocks=[{'t':'map','b':[.1,.1,.3,.2],'c':.99,'s':'ocr'},
                {'t':'Right paragraph one.','b':[.6,.1,.9,.2],'c':.99,'s':'ocr'},
                {'t':'Right paragraph two.','b':[.6,.4,.9,.5],'c':.99,'s':'ocr'}]
        payload={**raw_page(),**ocr_layout.arrange(blocks,100,200,{'writing_mode':'horizontal-ltr'})}
        original=layer.from_page(payload,'a'*64,'en')
        body_start=original['text'].index('Right paragraph one.')
        body=original['text'][body_start:]
        proposal=self.visual_proposal(original)
        proposal.update(additions=[{'region_id':'v'+'1'*16,'text':'H','box':[.2,.3,.3,.4],
            'quad':[[.2,.3],[.3,.3],[.3,.4],[.2,.4]],
            'evidence':{'crop_sha256':'f'*64,'reason':'Printed source glyph'}}],
            region_order=['b0','v'+'1'*16,'b1','b2'])
        accepted=layer.accept_proposal(original,proposal,actor='reviewer')
        self.assertEqual(accepted['text'][accepted['text'].index('Right paragraph one.'):],body)

    def test_visual_geometry_change_requires_exact_previous_geometry(self):
        original=layer.from_page(raw_page('1'),'a'*64,'en');proposal=self.visual_proposal(original)
        region=original['regions'][0]
        change={'region_id':'b0','before':'1','after':'B','evidence':{'crop_sha256':'e'*64,'reason':'Full source B glyph'},
                'geometry':{'before_box':region['box'],'before_quad':region['quad'],
                            'box':[.2,.1,.3,.2],'quad':[[.2,.1],[.3,.1],[.3,.2],[.2,.2]]}}
        proposal['replacements']=[change]
        accepted=layer.accept_proposal(original,proposal,actor='reviewer')
        self.assertEqual(accepted['regions'][0]['box'],[.2,.1,.3,.2])
        self.assertEqual(accepted['raw_sha256'],original['raw_sha256'])
        self.assertEqual(accepted['acceptances'][-1]['geometry_changes'][0]['original_region'],original['regions'][0])
        proposal['replacements'][0]['geometry']['before_box']=[0,0,1,1]
        with self.assertRaisesRegex(ValueError,'geometry'):
            layer.accept_proposal(original,proposal,actor='reviewer')

    def test_explicit_order_acceptance_preserves_regions_and_rejects_unpositioned_text(self):
        blocks = [{"t": "First", "b": [.1, .1, .4, .2], "c": .8, "s": "ocr"},
                  {"t": "Second", "b": [.6, .1, .9, .2], "c": .8, "s": "ocr"}]
        raw = {**raw_page(), **ocr_layout.arrange(blocks, 100, 200, include_writing_modes=True)}
        baseline = layer.from_page(raw, "a" * 64)
        order = [r["id"] for r in reversed(baseline["regions"])]
        proposal = {"version": 1, "kind": "pdf-text-correction", "base_generation": baseline["generation"],
                    "raw_sha256": baseline["raw_sha256"], "page_identity": baseline["page_identity"],
                    "replacements": [], "region_order": order}
        accepted = layer.accept_proposal(baseline, proposal, actor="reviewer")
        self.assertEqual(accepted["text"], "Second\nFirst")
        self.assertEqual([r["id"] for r in accepted["regions"]], order)
        self.assertEqual(accepted["regions"][0]["quad"], baseline["regions"][1]["quad"])
        self.assertEqual(baseline["revision"], "raw")
        for bad in ([order[0]], [order[0]] * 2, ["b999", order[0]]):
            with self.assertRaises(ValueError):
                layer.accept_proposal(baseline, {**proposal, "region_order": bad}, actor="reviewer")
        unmapped = copy.deepcopy(baseline)
        unmapped["text"] += "unpositioned"
        unmapped["generation"] = layer.digest({k: v for k, v in unmapped.items() if k != "generation"})
        with self.assertRaisesRegex(ValueError, "unpositioned"):
            layer.accept_proposal(unmapped, {**proposal, "base_generation": unmapped["generation"]}, actor="reviewer")

    def test_legacy_page_is_deterministic_and_does_not_mutate_raw(self):
        raw = raw_page()
        before = copy.deepcopy(raw)
        first = layer.from_page(raw, "a" * 64, "en")
        self.assertEqual(first, layer.from_page(raw, "a" * 64, "en"))
        self.assertEqual(raw, before)
        self.assertEqual(first["quality"], "unreviewed")
        self.assertEqual(first["regions"][0]["quad"], [[.1, .1], [.8, .1], [.8, .2], [.1, .2]])

    def test_arabic_hebrew_and_mixed_text_keep_logical_string(self):
        for text, expected in (("كتاب", "rtl"), ("שלום", "rtl"), ("كتاب test 123", "mixed")):
            with self.subTest(text=text):
                result = layer.from_page(raw_page(text, mode="horizontal-rtl"), "a" * 64)
                self.assertEqual(result["text"], text)
                self.assertEqual(result["regions"][0]["direction"], expected)
                self.assertIn("bidi-order-needs-review", result["review_flags"])

    def test_unicode_codepoint_offsets_include_non_bmp_and_combining_text(self):
        text = "A\U00020000e\u0301"
        result = layer.from_page(raw_page(text), "a" * 64)
        self.assertEqual(result["regions"][0]["end"], 4)
        self.assertEqual(result["text"], text)

    def test_mixed_region_writing_modes_survive_page_default(self):
        blocks = [{"t": "Header", "b": [.1, .01, .9, .06], "c": 1, "s": "ocr"},
                  {"t": "竖排正文", "b": [.7, .2, .75, .8], "c": 1, "s": "ocr"}]
        raw = {**raw_page(), **ocr_layout.arrange(blocks, 100, 200, {"regions": [
            {"box": [0, 0, 1, .1], "writing_mode": "horizontal-ltr"},
            {"box": [0, .1, 1, 1], "writing_mode": "vertical-rl"}]}, include_writing_modes=True)}
        result = layer.from_page(raw, "a" * 64)
        self.assertEqual([r["writing_mode"] for r in result["regions"]], ["horizontal-ltr", "vertical-rl"])

    def test_degenerate_or_out_of_bounds_quads_are_rejected(self):
        baseline = layer.from_page(raw_page(), "a" * 64)
        for quad in ([[.1, .1], [.2, .2], [.3, .3], [.4, .4]],
                     [[-.1, .1], [.8, .1], [.8, .2], [.1, .2]]):
            bad = copy.deepcopy(baseline)
            bad["regions"][0]["quad"] = quad
            bad["generation"] = layer.digest({k: v for k, v in bad.items() if k != "generation"})
            with self.assertRaises(ValueError):
                layer.validate(bad)

    def test_legacy_spans_get_region_modes_without_changing_raw_object(self):
        raw = raw_page("Text")
        raw["text_spans"][0].pop("writing_mode")
        before = copy.deepcopy(raw)
        result = layer.from_page(raw, "a" * 64, layout_options={"regions": [
            {"box": [0, 0, 1, 1], "writing_mode": "vertical-rl"}]})
        self.assertEqual(result["regions"][0]["writing_mode"], "vertical-rl")
        self.assertEqual(raw, before)

    def test_empty_recognition_is_not_claimed_to_be_blank(self):
        result = layer.from_page(raw_page(""), "a" * 64)
        self.assertEqual(result["processing"], "processed-empty")
        self.assertIn("empty-recognition-unverified", result["review_flags"])
        self.assertEqual(result["quality"], "unreviewed")

    def test_mismatched_native_text_does_not_get_false_offsets(self):
        raw = raw_page("native words")
        raw.update(source="native", text="different extractor order")
        result = layer.from_page(raw, "a" * 64)
        self.assertEqual(result["regions"], [])
        self.assertIn("text-span-mismatch", result["review_flags"])
        self.assertEqual(result["text"], "different extractor order")

    def test_low_confidence_and_bad_geometry_are_reviewed(self):
        raw = raw_page("Text", .4)
        self.assertIn("low-recognition-confidence", layer.from_page(raw, "a" * 64)["review_flags"])
        raw["text_spans"][0]["box"] = [0, 0, float("nan"), 1]
        # Nonfinite evidence is not allowed into content-addressed output.
        with self.assertRaises(ValueError):
            layer.from_page(raw, "a" * 64)

    def test_generation_and_geometry_tampering_are_rejected(self):
        raw = layer.from_page(raw_page(), "a" * 64)
        for change in ({"text": "altered"}, {"page": 2}, {"source_sha256": "b" * 64}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                layer.validate({**raw, **change})

    def test_explicit_acceptance_updates_offsets_without_rewriting_raw(self):
        blocks = [{"t": "First", "b": [.1, .1, .5, .2], "c": 1, "s": "ocr"},
                  {"t": "Second", "b": [.1, .3, .5, .4], "c": 1, "s": "ocr"}]
        raw = {**raw_page(), **ocr_layout.arrange(blocks, 100, 200, {"writing_mode": "horizontal-ltr"})}
        original = layer.from_page(raw, "a" * 64)
        proposal = {"kind": "pdf-text-correction", "version": 1,
                    "base_generation": original["generation"], "raw_sha256": original["raw_sha256"],
                    "page_identity": original["page_identity"],
                    "replacements": [{"region_id": "b0", "before": "First", "after": "First corrected"}]}
        accepted = layer.accept_proposal(original, proposal, actor="reviewer")
        self.assertEqual(original["text"], "First\n\nSecond")
        self.assertEqual(accepted["text"], "First corrected\n\nSecond")
        self.assertEqual(accepted["raw_sha256"], original["raw_sha256"])
        self.assertEqual(accepted["quality"], "partially-reviewed")
        self.assertEqual(accepted["regions"][0]["mapping_precision"], "corrected-block")
        for region in accepted["regions"]:
            self.assertEqual(accepted["text"][region["start"]:region["end"]], region["text"])
        with self.assertRaises(ValueError):
            layer.accept_proposal(accepted, proposal, actor="reviewer")

    def test_unknown_duplicate_and_stale_correction_regions_are_rejected(self):
        original = layer.from_page(raw_page(), "a" * 64)
        proposal = {"kind": "pdf-text-correction", "version": 1,
                    "base_generation": original["generation"], "raw_sha256": original["raw_sha256"],
                    "page_identity": original["page_identity"],
                    "replacements": [{"region_id": "b0", "before": "English", "after": "Correct"}]}
        for replacement in ([{"region_id": "missing", "before": "English", "after": "Correct"}],
                            proposal["replacements"] * 2,
                            [{"region_id": "b0", "before": "wrong", "after": "Correct"}]):
            with self.assertRaises(ValueError):
                layer.accept_proposal(original, {**proposal, "replacements": replacement}, actor="reviewer")


if __name__ == "__main__":
    unittest.main()
