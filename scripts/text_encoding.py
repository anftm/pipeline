"""Strict source-text normalization shared with the existing TXT publisher's scores.

Round trips prove byte preservation, not the language of an ambiguous codepage.
Ambiguous legacy inputs require an explicit per-source encoding override.
"""
from __future__ import annotations

import codecs
from collections import Counter
import hashlib
import re

try:
    from .publish_source_txt_once import STRICT_ENCODINGS, looks_like_lfs_pointer, text_quality_score
except ImportError:
    from publish_source_txt_once import STRICT_ENCODINGS, looks_like_lfs_pointer, text_quality_score

PROFILE = "strict-utf8-bom-v1"
UTF8_BOM = codecs.BOM_UTF8
COMMON_CJK = set("的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所民得经之进着等部家自理起现实都体制当本性应开合因由然前外政社义事相全与关各重新内正反明原利质向道命此变结解问意建公系军情者立代通题党程展料员革文总品活长求老基资级图统知组别期论运农指区战任处理世")

# Manually reviewed, user-authorized source repairs. These are not decoding
# guesses or a caller-supplied replacement API. Each recipe accepts only the
# complete original SHA and one exact byte span, retaining all other content.
REVIEWED_SOURCE_REPAIRS = {
    "mlmrl-1967-02-01-mojibake-v1": {
        "source_sha256": "da24e640b710afb49ba75a7a107047da066b202d29b0115d185729bbc5b34805",
        "offset": 9484,
        "original_text": "穹\0瘢\0炊\0哦荆\0驮闾ち巳嗣癫撇\0\0腔挂\0\0墒裁矗?/p>",
        "replacement_text": "民服务，反而放毒，就糟踏了人民财产，那还要它干什么？",
        "reference_url": "https://www.prisoncensorship.info/archive/etext/chinese/classics/jiangqing1966to1976/061-090/073.html",
        "reference_sha256": "0a293f7f424650fbf4d5e91b85dc369841961c04fe2420bc36adb520a5f099ee",
    },
    "mlmrl-1967-04-16-mojibake-v1": {
        "source_sha256": "7088b4a442c02e818ee63a26c9d30952fb1a78ce6fb35afe57aa75f071449f27",
        "offset": 9761,
        "original_text": "\0囊欢\0浴？\0淮未蠡岵荒苌钊氲母恪Ｔ谒枷肷希\0谡苎Х矫妫\0褂懈\0喙ぷ饕\0觯\0嬲\0\0鞑檠芯俊８闩碚嬲庑┡淹讲皇潜本┒\0翘旖蚋愕模\0比灰灿斜本┮恍┎牧峡垂\0恕Ｎ颐且\0\0徊礁呔倜\0蠖\0枷胛按蠛炱焯乇鹨\0汛蠓较蜃ソ簦\0鞑榕碚嫖侍夂苌伲\0\0奈侍庑枰\0阋桓恪?/p>",
        "replacement_text": (
            "样的一动脑。开一次大会不能深入的搞。在思想上，在哲学方面，还有更多工作要做，真正要调查研究。"
            "搞彭真这些叛徒不是北京而是天津搞的，当然也有北京一些材料看过了。"
            "我们要进一步高举毛泽东思想伟大红旗特别要把大方向抓紧，调查彭真问题很少，他的问题需要搞一搞。"
        ),
        "reference_url": "https://www.prisoncensorship.info/archive/etext/chinese/classics/jiangqing1966to1976/061-090/088.html",
        "reference_sha256": "997d95c4b48cb826561209963d5d77ea100d5366f36b538c61dcacd16d6e5774",
    },
    "mlmrl-1967-07-21-mojibake-v1": {
        "source_sha256": "d31061576c29a154388f04ebd2219f66d2cfef9921e7a3f2154ac9e825e1aeb3",
        "offset": 9887,
        "original_text": "\0啥荚谟涡校\0诱饫锟梢钥闯觯\0诿\0飨\0斓枷碌母锩\0褐谑钦嬲\0祷さ持醒搿19.\0飨\0摹?/p>",
        "replacement_text": "命派都在游行，从这里可以看出，在毛主席领导下的革命群众是真正拥护党中央、毛主席的。\n\n",
        "reference_url": "https://www.prisoncensorship.info/archive/etext/chinese/classics/jiangqing1966to1976/091-120/100.html",
        "reference_sha256": "0b7ff0cdfe2f7d91fa86afa431f53f987b1565ad127439920df576b526b15d0f",
    },
    "mlmrl-1967-09-01-mojibake-v1": {
        "source_sha256": "dd446bcf91636ffc0b8254311191641c9fd5bdf306a80e1cce7005880d72a32c",
        "offset": 9831,
        "original_text": "打鼓忝牵\0褂泻\0\0А(9)醮蟊觥(8)泛窭家彩钦庋\0；褂屑父鋈耍\0颐羌遣磺辶耍\0灰\0灰坏愎ρ沟么\0还\0\0矗\0\0焯旒亲∥\0嗣窳(12)鹿ΑＮ医驳貌欢裕\0梢曰鹕眨\0梢耘诤洹4.虻苟夹小５\0俏沂锹\0橙惹槎酝\0久墙不啊?/p>",
        "replacement_text": "打倒你们，还有韩爱晶、王大宾、谭厚兰也是这样。还有几个人，我们记不清了，不要被一点功压得喘不过气来，要天天记住为人民立新功。我讲得不对，可以火烧，可以炮轰、打倒都行。但是我是满怀热情对同志们讲话。\n\n",
        "reference_url": "https://www.prisoncensorship.info/archive/etext/chinese/classics/jiangqing1966to1976/091-120/104.html",
        "reference_sha256": "ccfe1bd8d5d6cc6fffb8c62d6c503ab1824be75c5a8e0c51fedb66b056a9d83e",
    },
    "mlmrl-1968-03-21-mojibake-v1": {
        "source_sha256": "6c1dc88c70a92b868186d19d9a4a8ce19b5a63dfd0961cfd80015974c66cbd11",
        "offset": 16097,
        "original_text": "瑁\u0000乐沟腥私\u0000ィ\u0000苹凳だ\u0000\u0000怠＝\u0000嗤\u0000尽熬拧の濉苯不八倒\u0000\u0000\u0000瞧笸级\u0000∫悦\u0000飨\u0000\u0000鳎\u0000指敝飨\u0000\u0000 ǖ奈薏\u0000准端玖畈浚\u0000\u0000〗伙啪\u0000\u0000\u0000∥颐呛焐\u0000抡\u0000 ā４佣氛\u0000形颐侵\u0000溃\u0000跎倨妗5.诵∑轿\u0000裁茨艹て谝\u0000卦诘衬谀兀烤褪撬\u0000撬Ａ矫媸址 ǎ\u0000蜃拧昂炱臁狈春炱欤\u0000颐潜匦胙勖餍牧粒\u0000朴谑镀扑\u0000堑拿婺浚\u0000\u0000揽抗愦蟾锩\u0000褐冢\u0000揽咳嗣窠伙啪\u0000\u0000\u0000\u0000鱿嘈牛\u0000\u0000鲆揽浚\u0000湍苁镀埔磺谢等恕９膊\u0000橙斯饷骼诼洌\u0000炊粤矫媾桑\u0000矫媸址 ǎ\u0000醯怂Ａ矫媸址 ǎ\u0000〗\u0000峋龇炊运\u0000堑奶匚窳矫媸址 ǎ\u0000炊愿阋跄被疃\u0000?/p>",
        "replacement_text": "惕，防止敌人进攻，破坏胜利果实。江青同志“九·五”讲话说过，他们企图动摇以毛主席为主，林副主席为辅的无产阶级司令部，动摇解放军，动摇我们红色新政权。从斗争中我们知道，刘少奇、邓小平为什么能长期隐藏在党内呢？就是他们耍两面手法，打着“红旗”反红旗，我们必须眼明心亮，善于识破他们的面目，要依靠广大革命群众，依靠人民解放军，要三个相信，三个依靠，就能识破一切坏人。共产党人光明磊落，反对两面派，两面手法，刘邓耍两面手法，小将坚决反对他们的特务两面手法，反对搞阴谋活动。\n\n",
        "reference_url": "https://www.prisoncensorship.info/archive/etext/chinese/classics/jiangqing1966to1976/121-160/125.html",
        "reference_sha256": "90daefc2119814ff52a0dda96e1ca1d5eb93efb84b799e6b6cdd349f997867ea",
    },
    # These seven 马恩全集 sources are strict UTF-8 text whose tails contain
    # PDF image bytes decoded as text. Each reviewed recipe replaces one exact
    # suffix with the illustrated-section captions found between the image
    # streams; no image bytes are treated as text. The full original SHA and
    # the exact suffix SHA pin both the source and the removed span, and
    # build_one retains the complete original as provenance.
    "mlmrl-teachers-vol17-binary-images-v1": {
        "source_sha256": "f570cd4e65c6404b3b61e91ff796b08a29349241a39687c5e6487b578beea76c",
        "mode": "suffix-strip",
        "offset_chars": 537609,
        "original_suffix_characters": 710971,
        "original_suffix_sha256": "a8446bfb7df7c45389f50f98db44702460b73f72e9d426d0d320c0521e8435ba",
        "replacement_text": "==========\n\n## 巴黎的包围（1870—1871年）…………………………………………………………………………………………234—235\n\n==========\n\n## 普法战争（1870—1871年）……………………………………………………………………………………………276—277\n\n==========\n\n## 巴黎公社战斗行动图………………………………………………………………………………………………………380—381\n\n",
        "replacement_sha256": "4f3a3b9e0e8e0cab9f820b7f90dda66b20308d2c9820002046a5ff2d6795466d",
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/Teachers/blob/6a915db1d3571c4fdd32d409e06e40704a9fd28f/A1%20%E9%A9%AC%E5%85%8B%E6%80%9D%26%E6%81%A9%E6%A0%BC%E6%96%AF/01-05%20%E9%A9%AC%E5%85%8B%E6%80%9D%E6%81%A9%E6%A0%BC%E6%96%AF%E5%85%A8%E9%9B%86%20%E6%96%87%E5%BA%93%E7%89%88/%E9%A9%AC%E6%81%A9%E5%85%A8%E9%9B%86%20%E7%AC%AC17%E5%8D%B7.txt",
        "reference_sha256": "f570cd4e65c6404b3b61e91ff796b08a29349241a39687c5e6487b578beea76c",
        "reference_kind": "original-source-binary-audit",
        "image_spans": [{"start": 537609, "end": 709429, "characters": 171820}, {"start": 709504, "end": 825825, "characters": 116321}, {"start": 825900, "end": 1144775, "characters": 318875}, {"start": 1144847, "end": 1248580, "characters": 103733}],
    },
    "mlmrl-teachers-vol19-binary-images-v1": {
        "source_sha256": "89ce21133aa4514e8a20893346387b22ec360693bbe8917e4cee968dc44a6c16",
        "mode": "suffix-strip",
        "offset_chars": 458527,
        "original_suffix_characters": 311608,
        "original_suffix_sha256": "08826549e28e9e1cac4e3086c89302763a3a0d58e639b274752b81fa6b4c4d1a",
        "replacement_text": "==========\n\n## 弗·恩格斯“法兰克方言”一文中的日耳曼方言图………………………………………………………………………580—581\n\n",
        "replacement_sha256": "c402dba73efd6b8bde0575d53ba5b01a9839bc3bf6e675454c4a41a3c8887486",
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/Teachers/blob/6a915db1d3571c4fdd32d409e06e40704a9fd28f/A1%20%E9%A9%AC%E5%85%8B%E6%80%9D%26%E6%81%A9%E6%A0%BC%E6%96%AF/01-05%20%E9%A9%AC%E5%85%8B%E6%80%9D%E6%81%A9%E6%A0%BC%E6%96%AF%E5%85%A8%E9%9B%86%20%E6%96%87%E5%BA%93%E7%89%88/%E9%A9%AC%E6%81%A9%E5%85%A8%E9%9B%86%20%E7%AC%AC19%E5%8D%B7.txt",
        "reference_sha256": "89ce21133aa4514e8a20893346387b22ec360693bbe8917e4cee968dc44a6c16",
        "reference_kind": "original-source-binary-audit",
        "image_spans": [{"start": 458527, "end": 607840, "characters": 149313}, {"start": 607913, "end": 770135, "characters": 162222}],
    },
    "mlmrl-teachers-vol27-binary-images-v1": {
        "source_sha256": "976194efce438bbfb6f4d14cf997f92a8c589d86c3dc5ea7db64f3e4e07918c5",
        "mode": "suffix-strip",
        "offset_chars": 514664,
        "original_suffix_characters": 87240,
        "original_suffix_sha256": "6c0c5aedf14faa062c58ea9fce4fb8050583d3f2ca009475e521dbbb36822c20",
        "replacement_text": "==========\n\n## 弗·恩格斯所作的对普鲁士国王弗里德里希一威廉四世的讽刺画(1848年)…………………………………508-509\n\n",
        "replacement_sha256": "5c845d75c6779b327bba16c2030eb925d3475d0c9685637d57e91f3e7c8f3a53",
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/Teachers/blob/6a915db1d3571c4fdd32d409e06e40704a9fd28f/A1%20%E9%A9%AC%E5%85%8B%E6%80%9D%26%E6%81%A9%E6%A0%BC%E6%96%AF/01-05%20%E9%A9%AC%E5%85%8B%E6%80%9D%E6%81%A9%E6%A0%BC%E6%96%AF%E5%85%A8%E9%9B%86%20%E6%96%87%E5%BA%93%E7%89%88/%E9%A9%AC%E6%81%A9%E5%85%A8%E9%9B%86%20%E7%AC%AC27%E5%8D%B7.txt",
        "reference_sha256": "976194efce438bbfb6f4d14cf997f92a8c589d86c3dc5ea7db64f3e4e07918c5",
        "reference_kind": "original-source-binary-audit",
        "image_spans": [{"start": 514664, "end": 570053, "characters": 55389}, {"start": 570125, "end": 601904, "characters": 31779}],
    },
    "mlmrl-teachers-vol29-binary-images-v1": {
        "source_sha256": "b8941a33a36f8204b3b8c5e15505616109024b99b9760c4f6967e097a066e772",
        "mode": "suffix-strip",
        "offset_chars": 528318,
        "original_suffix_characters": 96278,
        "original_suffix_sha256": "01bade35a88a407800c8cb9123368292e0829085c780db21658ee2d66472f28e",
        "replacement_text": "",
        "replacement_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/Teachers/blob/6a915db1d3571c4fdd32d409e06e40704a9fd28f/A1%20%E9%A9%AC%E5%85%8B%E6%80%9D%26%E6%81%A9%E6%A0%BC%E6%96%AF/01-05%20%E9%A9%AC%E5%85%8B%E6%80%9D%E6%81%A9%E6%A0%BC%E6%96%AF%E5%85%A8%E9%9B%86%20%E6%96%87%E5%BA%93%E7%89%88/%E9%A9%AC%E6%81%A9%E5%85%A8%E9%9B%86%20%E7%AC%AC29%E5%8D%B7.txt",
        "reference_sha256": "b8941a33a36f8204b3b8c5e15505616109024b99b9760c4f6967e097a066e772",
        "reference_kind": "original-source-binary-audit",
        "image_spans": [{"start": 528318, "end": 624596, "characters": 96278}],
    },
    "mlmrl-teachers-vol30-binary-images-v1": {
        "source_sha256": "7fff85931cb404780097a3d5650396e64cfdfe0d8e0b8814ed76a95b12d28998",
        "mode": "suffix-strip",
        "offset_chars": 559830,
        "original_suffix_characters": 61440,
        "original_suffix_sha256": "07026424e1d0d94729fde5cbee21fd2983f9be11068dff38879b6a6ca9478043",
        "replacement_text": "",
        "replacement_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/Teachers/blob/6a915db1d3571c4fdd32d409e06e40704a9fd28f/A1%20%E9%A9%AC%E5%85%8B%E6%80%9D%26%E6%81%A9%E6%A0%BC%E6%96%AF/01-05%20%E9%A9%AC%E5%85%8B%E6%80%9D%E6%81%A9%E6%A0%BC%E6%96%AF%E5%85%A8%E9%9B%86%20%E6%96%87%E5%BA%93%E7%89%88/%E9%A9%AC%E6%81%A9%E5%85%A8%E9%9B%86%20%E7%AC%AC30%E5%8D%B7.txt",
        "reference_sha256": "7fff85931cb404780097a3d5650396e64cfdfe0d8e0b8814ed76a95b12d28998",
        "reference_kind": "original-source-binary-audit",
        "image_spans": [{"start": 559830, "end": 621270, "characters": 61440}],
    },
    "mlmrl-teachers-vol31-binary-images-v1": {
        "source_sha256": "a67ded24b55b65dd68d858147932bfea48cc2cd1b25eb2a827c6a611b9206705",
        "mode": "suffix-strip",
        "offset_chars": 572724,
        "original_suffix_characters": 60989,
        "original_suffix_sha256": "74ba6b8affcc2e77ff8e3aabbae23e184e210669bd89bf2ce89153d9628aae8e",
        "replacement_text": "",
        "replacement_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/Teachers/blob/6a915db1d3571c4fdd32d409e06e40704a9fd28f/A1%20%E9%A9%AC%E5%85%8B%E6%80%9D%26%E6%81%A9%E6%A0%BC%E6%96%AF/01-05%20%E9%A9%AC%E5%85%8B%E6%80%9D%E6%81%A9%E6%A0%BC%E6%96%AF%E5%85%A8%E9%9B%86%20%E6%96%87%E5%BA%93%E7%89%88/%E9%A9%AC%E6%81%A9%E5%85%A8%E9%9B%86%20%E7%AC%AC31%E5%8D%B7.txt",
        "reference_sha256": "a67ded24b55b65dd68d858147932bfea48cc2cd1b25eb2a827c6a611b9206705",
        "reference_kind": "original-source-binary-audit",
        "image_spans": [{"start": 572724, "end": 633713, "characters": 60989}],
    },
    "mlmrl-teachers-vol43-binary-images-v1": {
        "source_sha256": "de22762fd4c85e4346b10982c3537e41585a533011fc8e5c38b90897ebcd8880",
        "mode": "suffix-strip",
        "offset_chars": 429451,
        "original_suffix_characters": 373854,
        "original_suffix_sha256": "5673aa0cdf23e5469ff0d00119d9a0f179537854b63f298d5cc40291f7e63151",
        "replacement_text": "",
        "replacement_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/Teachers/blob/6a915db1d3571c4fdd32d409e06e40704a9fd28f/A1%20%E9%A9%AC%E5%85%8B%E6%80%9D%26%E6%81%A9%E6%A0%BC%E6%96%AF/01-05%20%E9%A9%AC%E5%85%8B%E6%80%9D%E6%81%A9%E6%A0%BC%E6%96%AF%E5%85%A8%E9%9B%86%20%E6%96%87%E5%BA%93%E7%89%88/%E9%A9%AC%E6%81%A9%E5%85%A8%E9%9B%86%20%E7%AC%AC43%E5%8D%B7.txt",
        "reference_sha256": "de22762fd4c85e4346b10982c3537e41585a533011fc8e5c38b90897ebcd8880",
        "reference_kind": "original-source-binary-audit",
        "image_spans": [{"start": 429451, "end": 803305, "characters": 373854}],
    },
    # Three batch-12 sources contain only isolated, invisible control bytes in
    # otherwise complete text. These recipes name the exact source SHA, source
    # encoding, control code points and character offsets. Only those controls
    # are removed; every other character is retained, and the complete original
    # is retained by build_one as provenance.
    "mlmrl-teachers-lenin-vol42-control-v1": {
        "source_sha256": "5fd9c39e11b3f54b7cf414d69dc11a5bd59713b73a7ff6bc652cd9eccf130475",
        "mode": "control-strip",
        "source_encoding": "utf-8",
        "control_codepoints": ["\u001e"],
        "control_offsets": [335660],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/Teachers/blob/6a915db1d3571c4fdd32d409e06e40704a9fd28f/A2%20%E5%88%97%E5%AE%81/01-06%20%E5%88%97%E5%AE%81%E5%85%A8%E9%9B%86%20%E6%96%87%E5%BA%93%E7%89%88/%E5%88%97%E5%AE%81%E5%85%A8%E9%9B%86%20%E7%AC%AC42%E5%8D%B7.txt",
        "reference_sha256": "5fd9c39e11b3f54b7cf414d69dc11a5bd59713b73a7ff6bc652cd9eccf130475",
        "reference_kind": "original-source-control-audit",
    },
    "mlmrl-teachers-lenin-cw45-control-v1": {
        "source_sha256": "7083023e0a961cac23eba03a3fe62d146be4cfec4ca25e542f859d4b76863846",
        "mode": "control-strip",
        "source_encoding": "utf-8",
        "control_codepoints": ["\u000e", "\u000e", "\u000e"],
        "control_offsets": [128947, 161032, 221769],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/Teachers/blob/6a915db1d3571c4fdd32d409e06e40704a9fd28f/A2%20%E5%88%97%E5%AE%81/01-10%20%E5%88%97%E5%AE%81%E5%85%A8%E9%9B%86%20%E8%8B%B1%E6%96%87%E7%89%88%20%E6%96%87%E5%BA%93%E7%89%88/Volume%2045.txt",
        "reference_sha256": "7083023e0a961cac23eba03a3fe62d146be4cfec4ca25e542f859d4b76863846",
        "reference_kind": "original-source-control-audit",
    },
    "mlmrl-vomebook-476195375-control-v1": {
        "source_sha256": "63728a12b71b4a295919531d7566f0489501b6fe12be90850f7dfd30759b5e5d",
        "mode": "control-strip",
        "source_encoding": "gb18030",
        "control_codepoints": ["\u0010"],
        "control_offsets": [276532],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/VOMEBOOK/blob/7f338fb4af738ccd6e8353b90db9c16023497555/%E9%A9%AC%E5%88%97%E4%B9%8B%E5%A3%B0ebook%E5%B0%8F%E7%BB%84%E7%AC%AC%E4%B8%89%E6%89%B9%E6%88%90%E6%9E%9C%E9%9B%86%E6%88%90%E8%B5%9E%E5%8A%A9%E9%83%A8%E5%88%86/jiang2-%E8%B4%A1%E7%8C%AE/%E5%85%B3%E4%BA%8E%E8%BD%AC%E5%9F%BA%E5%9B%A0%E5%85%AC%E5%AE%B3%E7%9A%84%E7%BD%91%E9%A1%B5%E7%AD%89%E8%B5%84%E6%96%99/476195375.txt",
        "reference_sha256": "63728a12b71b4a295919531d7566f0489501b6fe12be90850f7dfd30759b5e5d",
        "reference_kind": "original-source-control-audit",
    },
    "mlmrl-vomebook-gongnongbing-01-marker-v1": {
        "source_sha256": "41c07731a679bdc4fdcdf4878ccdbdc3cf987c2910bb980234bf7c981c07ff62",
        "mode": "legacy-marker-strip",
        "source_encoding": "gb18030",
        "marker_hex": "f8fa0702cb35f501",
        "marker_count": 10,
        "marker_offsets": [1025, 2057, 3089, 4121, 5153, 6185, 7217, 8249, 9281, 10313],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/VOMEBOOK/blob/7f338fb4af738ccd6e8353b90db9c16023497555/%E9%A9%AC%E5%88%97%E4%B9%8B%E5%A3%B0ebook%E5%B0%8F%E7%BB%84%E7%AC%AC%E4%BA%8C%E6%89%B9%E6%88%90%E6%9E%9C%E9%9B%86%E6%88%90%E8%B5%9E%E5%8A%A9%E9%83%A8%E5%88%86/jiang/%E4%B8%AD%E5%9B%BD%E7%A4%BE%E4%BC%9A%E4%B8%BB%E4%B9%89%E6%97%B6%E4%BB%A3%E7%9A%84%E6%95%99%E8%82%B2%E9%9D%A9%E5%91%BD%20%E7%A0%94%E7%A9%B6%E8%B5%84%E6%96%99%E9%9B%86%E6%88%90pdf%EF%BC%88%E8%BF%91800%E7%A7%8D%EF%BC%89/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%A4%A7%E5%AD%A6%E7%94%9F/%E4%BC%98%E7%A7%80%E5%B7%A5%E5%86%9C%E5%85%B5%E5%AD%A6%E5%91%98%E7%9A%84%E5%85%B1%E5%90%8C%E7%89%B9%E7%82%B9.txt",
        "reference_sha256": "41c07731a679bdc4fdcdf4878ccdbdc3cf987c2910bb980234bf7c981c07ff62",
        "reference_kind": "original-source-binary-marker-audit",
    },
    "mlmrl-vomebook-gongnongbing-02-marker-v1": {
        "source_sha256": "a4b610eea8062b8d22d44896e507dd21596b55c8c344df2667b595f095fc57be",
        "mode": "legacy-marker-strip",
        "source_encoding": "gb18030",
        "marker_hex": "f8fa0702cb35f501",
        "marker_count": 16,
        "marker_offsets": [1025, 2057, 3089, 4121, 5153, 6185, 7217, 8249, 9281, 10313, 11345, 12377, 13409, 14441, 15473, 16505],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/VOMEBOOK/blob/7f338fb4af738ccd6e8353b90db9c16023497555/%E9%A9%AC%E5%88%97%E4%B9%8B%E5%A3%B0ebook%E5%B0%8F%E7%BB%84%E7%AC%AC%E4%BA%8C%E6%89%B9%E6%88%90%E6%9E%9C%E9%9B%86%E6%88%90%E8%B5%9E%E5%8A%A9%E9%83%A8%E5%88%86/jiang/%E4%B8%AD%E5%9B%BD%E7%A4%BE%E4%BC%9A%E4%B8%BB%E4%B9%89%E6%97%B6%E4%BB%A3%E7%9A%84%E6%95%99%E8%82%B2%E9%9D%A9%E5%91%BD%20%E7%A0%94%E7%A9%B6%E8%B5%84%E6%96%99%E9%9B%86%E6%88%90pdf%EF%BC%88%E8%BF%91800%E7%A7%8D%EF%BC%89/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%A4%A7%E5%AD%A6%E7%94%9F/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%AD%A6%E5%91%98%E4%B8%AD%E7%9A%84%E6%9D%B0%E5%87%BA%E4%BA%BA%E6%89%8D.txt",
        "reference_sha256": "a4b610eea8062b8d22d44896e507dd21596b55c8c344df2667b595f095fc57be",
        "reference_kind": "original-source-binary-marker-audit",
    },
    "mlmrl-vomebook-gongnongbing-03-marker-v1": {
        "source_sha256": "975aecd3d7389f6e1b3526bdb682aa1e3b137bd33112466cf5d769a496a30984",
        "mode": "legacy-marker-strip",
        "source_encoding": "gb18030",
        "marker_hex": "f8fa0702cb35f501",
        "marker_count": 1,
        "marker_offsets": [1025],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/VOMEBOOK/blob/7f338fb4af738ccd6e8353b90db9c16023497555/%E9%A9%AC%E5%88%97%E4%B9%8B%E5%A3%B0ebook%E5%B0%8F%E7%BB%84%E7%AC%AC%E4%BA%8C%E6%89%B9%E6%88%90%E6%9E%9C%E9%9B%86%E6%88%90%E8%B5%9E%E5%8A%A9%E9%83%A8%E5%88%86/jiang/%E4%B8%AD%E5%9B%BD%E7%A4%BE%E4%BC%9A%E4%B8%BB%E4%B9%89%E6%97%B6%E4%BB%A3%E7%9A%84%E6%95%99%E8%82%B2%E9%9D%A9%E5%91%BD%20%E7%A0%94%E7%A9%B6%E8%B5%84%E6%96%99%E9%9B%86%E6%88%90pdf%EF%BC%88%E8%BF%91800%E7%A7%8D%EF%BC%89/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%A4%A7%E5%AD%A6%E7%94%9F/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%AD%A6%E5%91%98%E7%9A%84%E4%BC%98%E7%A7%80%E4%BB%A3%E8%A1%A8----%E4%BE%AF%E5%87%A1%E5%87%A1%E9%99%A2%E5%A3%AB.txt",
        "reference_sha256": "975aecd3d7389f6e1b3526bdb682aa1e3b137bd33112466cf5d769a496a30984",
        "reference_kind": "original-source-binary-marker-audit",
    },
    "mlmrl-vomebook-gongnongbing-04-marker-v1": {
        "source_sha256": "ce604b101be5928e60cfec304a2c4b367bfd5abc40d024c7d374ed2d7d807e03",
        "mode": "legacy-marker-strip",
        "source_encoding": "gb18030",
        "marker_hex": "f8fa0702cb35f501",
        "marker_count": 2,
        "marker_offsets": [1025, 2057],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/VOMEBOOK/blob/7f338fb4af738ccd6e8353b90db9c16023497555/%E9%A9%AC%E5%88%97%E4%B9%8B%E5%A3%B0ebook%E5%B0%8F%E7%BB%84%E7%AC%AC%E4%BA%8C%E6%89%B9%E6%88%90%E6%9E%9C%E9%9B%86%E6%88%90%E8%B5%9E%E5%8A%A9%E9%83%A8%E5%88%86/jiang/%E4%B8%AD%E5%9B%BD%E7%A4%BE%E4%BC%9A%E4%B8%BB%E4%B9%89%E6%97%B6%E4%BB%A3%E7%9A%84%E6%95%99%E8%82%B2%E9%9D%A9%E5%91%BD%20%E7%A0%94%E7%A9%B6%E8%B5%84%E6%96%99%E9%9B%86%E6%88%90pdf%EF%BC%88%E8%BF%91800%E7%A7%8D%EF%BC%89/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%A4%A7%E5%AD%A6%E7%94%9F/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%AD%A6%E5%91%98%E7%9A%84%E4%BC%98%E7%A7%80%E4%BB%A3%E8%A1%A8----%E5%86%AF%E9%95%BF%E6%A0%B9%E6%95%99%E6%8E%88.txt",
        "reference_sha256": "ce604b101be5928e60cfec304a2c4b367bfd5abc40d024c7d374ed2d7d807e03",
        "reference_kind": "original-source-binary-marker-audit",
    },
    "mlmrl-vomebook-gongnongbing-05-marker-v1": {
        "source_sha256": "8e52bcee214f448ac24aad805d6a827d35a48b3866d20aedfe3f724a7c5c1b4e",
        "mode": "legacy-marker-strip",
        "source_encoding": "gb18030",
        "marker_hex": "f8fa0702cb35f501",
        "marker_count": 1,
        "marker_offsets": [1025],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/VOMEBOOK/blob/7f338fb4af738ccd6e8353b90db9c16023497555/%E9%A9%AC%E5%88%97%E4%B9%8B%E5%A3%B0ebook%E5%B0%8F%E7%BB%84%E7%AC%AC%E4%BA%8C%E6%89%B9%E6%88%90%E6%9E%9C%E9%9B%86%E6%88%90%E8%B5%9E%E5%8A%A9%E9%83%A8%E5%88%86/jiang/%E4%B8%AD%E5%9B%BD%E7%A4%BE%E4%BC%9A%E4%B8%BB%E4%B9%89%E6%97%B6%E4%BB%A3%E7%9A%84%E6%95%99%E8%82%B2%E9%9D%A9%E5%91%BD%20%E7%A0%94%E7%A9%B6%E8%B5%84%E6%96%99%E9%9B%86%E6%88%90pdf%EF%BC%88%E8%BF%91800%E7%A7%8D%EF%BC%89/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%A4%A7%E5%AD%A6%E7%94%9F/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%AD%A6%E5%91%98%E7%9A%84%E4%BC%98%E7%A7%80%E4%BB%A3%E8%A1%A8----%E5%88%98%E5%A5%87%E8%91%86.txt",
        "reference_sha256": "8e52bcee214f448ac24aad805d6a827d35a48b3866d20aedfe3f724a7c5c1b4e",
        "reference_kind": "original-source-binary-marker-audit",
    },
    "mlmrl-vomebook-gongnongbing-06-marker-v1": {
        "source_sha256": "3c9a6b0bb103304fcec6c44c2d6967d6538d433573d20fe03341e1c9c6f8e977",
        "mode": "legacy-marker-strip",
        "source_encoding": "gb18030",
        "marker_hex": "f8fa0702cb35f501",
        "marker_count": 1,
        "marker_offsets": [1025],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/VOMEBOOK/blob/7f338fb4af738ccd6e8353b90db9c16023497555/%E9%A9%AC%E5%88%97%E4%B9%8B%E5%A3%B0ebook%E5%B0%8F%E7%BB%84%E7%AC%AC%E4%BA%8C%E6%89%B9%E6%88%90%E6%9E%9C%E9%9B%86%E6%88%90%E8%B5%9E%E5%8A%A9%E9%83%A8%E5%88%86/jiang/%E4%B8%AD%E5%9B%BD%E7%A4%BE%E4%BC%9A%E4%B8%BB%E4%B9%89%E6%97%B6%E4%BB%A3%E7%9A%84%E6%95%99%E8%82%B2%E9%9D%A9%E5%91%BD%20%E7%A0%94%E7%A9%B6%E8%B5%84%E6%96%99%E9%9B%86%E6%88%90pdf%EF%BC%88%E8%BF%91800%E7%A7%8D%EF%BC%89/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%A4%A7%E5%AD%A6%E7%94%9F/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%AD%A6%E5%91%98%E7%9A%84%E4%BC%98%E7%A7%80%E4%BB%A3%E8%A1%A8----%E6%9D%A8%E5%8D%AB%E9%99%A2%E5%A3%AB.txt",
        "reference_sha256": "3c9a6b0bb103304fcec6c44c2d6967d6538d433573d20fe03341e1c9c6f8e977",
        "reference_kind": "original-source-binary-marker-audit",
    },
    "mlmrl-vomebook-gongnongbing-07-marker-v1": {
        "source_sha256": "8b1a0f534226b5fc00131b08eb2838e02f29f216f3d591a8a850cb4e1df963fc",
        "mode": "legacy-marker-strip",
        "source_encoding": "gb18030",
        "marker_hex": "f8fa0702cb35f501",
        "marker_count": 6,
        "marker_offsets": [1025, 2057, 3089, 4121, 5153, 6185],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/VOMEBOOK/blob/7f338fb4af738ccd6e8353b90db9c16023497555/%E9%A9%AC%E5%88%97%E4%B9%8B%E5%A3%B0ebook%E5%B0%8F%E7%BB%84%E7%AC%AC%E4%BA%8C%E6%89%B9%E6%88%90%E6%9E%9C%E9%9B%86%E6%88%90%E8%B5%9E%E5%8A%A9%E9%83%A8%E5%88%86/jiang/%E4%B8%AD%E5%9B%BD%E7%A4%BE%E4%BC%9A%E4%B8%BB%E4%B9%89%E6%97%B6%E4%BB%A3%E7%9A%84%E6%95%99%E8%82%B2%E9%9D%A9%E5%91%BD%20%E7%A0%94%E7%A9%B6%E8%B5%84%E6%96%99%E9%9B%86%E6%88%90pdf%EF%BC%88%E8%BF%91800%E7%A7%8D%EF%BC%89/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%A4%A7%E5%AD%A6%E7%94%9F/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%AD%A6%E5%91%98%E7%9A%84%E4%BC%98%E7%A7%80%E4%BB%A3%E8%A1%A8----%E6%9D%A8%E6%9C%88%E6%AC%A3.txt",
        "reference_sha256": "8b1a0f534226b5fc00131b08eb2838e02f29f216f3d591a8a850cb4e1df963fc",
        "reference_kind": "original-source-binary-marker-audit",
    },
    "mlmrl-vomebook-gongnongbing-08-marker-v1": {
        "source_sha256": "f36e761f4f5046dac84550faf3540c3babd5c3f7d6b05b392d192e8db39857c8",
        "mode": "legacy-marker-strip",
        "source_encoding": "gb18030",
        "marker_hex": "f8fa0702cb35f501",
        "marker_count": 3,
        "marker_offsets": [1025, 2057, 3089],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/VOMEBOOK/blob/7f338fb4af738ccd6e8353b90db9c16023497555/%E9%A9%AC%E5%88%97%E4%B9%8B%E5%A3%B0ebook%E5%B0%8F%E7%BB%84%E7%AC%AC%E4%BA%8C%E6%89%B9%E6%88%90%E6%9E%9C%E9%9B%86%E6%88%90%E8%B5%9E%E5%8A%A9%E9%83%A8%E5%88%86/jiang/%E4%B8%AD%E5%9B%BD%E7%A4%BE%E4%BC%9A%E4%B8%BB%E4%B9%89%E6%97%B6%E4%BB%A3%E7%9A%84%E6%95%99%E8%82%B2%E9%9D%A9%E5%91%BD%20%E7%A0%94%E7%A9%B6%E8%B5%84%E6%96%99%E9%9B%86%E6%88%90pdf%EF%BC%88%E8%BF%91800%E7%A7%8D%EF%BC%89/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%A4%A7%E5%AD%A6%E7%94%9F/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%AD%A6%E5%91%98%E7%9A%84%E4%BC%98%E7%A7%80%E4%BB%A3%E8%A1%A8----%E6%AF%95%E6%B7%91%E6%95%8F%20%E6%96%87%E5%AD%A6%E7%9A%84%E7%99%BD%E8%A1%A3%E5%A4%A9%E4%BD%BF.txt",
        "reference_sha256": "f36e761f4f5046dac84550faf3540c3babd5c3f7d6b05b392d192e8db39857c8",
        "reference_kind": "original-source-binary-marker-audit",
    },
    "mlmrl-vomebook-gongnongbing-09-marker-v1": {
        "source_sha256": "6315e8e0389b1a47213bac077dd6054e86bd46b9369834365a53da92e7049be7",
        "mode": "legacy-marker-strip",
        "source_encoding": "gb18030",
        "marker_hex": "f8fa0702cb35f501",
        "marker_count": 3,
        "marker_offsets": [1025, 2057, 3089],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/VOMEBOOK/blob/7f338fb4af738ccd6e8353b90db9c16023497555/%E9%A9%AC%E5%88%97%E4%B9%8B%E5%A3%B0ebook%E5%B0%8F%E7%BB%84%E7%AC%AC%E4%BA%8C%E6%89%B9%E6%88%90%E6%9E%9C%E9%9B%86%E6%88%90%E8%B5%9E%E5%8A%A9%E9%83%A8%E5%88%86/jiang/%E4%B8%AD%E5%9B%BD%E7%A4%BE%E4%BC%9A%E4%B8%BB%E4%B9%89%E6%97%B6%E4%BB%A3%E7%9A%84%E6%95%99%E8%82%B2%E9%9D%A9%E5%91%BD%20%E7%A0%94%E7%A9%B6%E8%B5%84%E6%96%99%E9%9B%86%E6%88%90pdf%EF%BC%88%E8%BF%91800%E7%A7%8D%EF%BC%89/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%A4%A7%E5%AD%A6%E7%94%9F/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%AD%A6%E5%91%98%E7%9A%84%E4%BC%98%E7%A7%80%E4%BB%A3%E8%A1%A8----%E7%99%BD%E6%98%A5%E7%A4%BC%E9%99%A2%E5%A3%AB.txt",
        "reference_sha256": "6315e8e0389b1a47213bac077dd6054e86bd46b9369834365a53da92e7049be7",
        "reference_kind": "original-source-binary-marker-audit",
    },
    "mlmrl-vomebook-gongnongbing-10-marker-v1": {
        "source_sha256": "694df25c2142e13cd79b460719e457916eda69a79c64d150d9ab2b7961772302",
        "mode": "legacy-marker-strip",
        "source_encoding": "gb18030",
        "marker_hex": "f8fa0702cb35f501",
        "marker_count": 1,
        "marker_offsets": [1025],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/VOMEBOOK/blob/7f338fb4af738ccd6e8353b90db9c16023497555/%E9%A9%AC%E5%88%97%E4%B9%8B%E5%A3%B0ebook%E5%B0%8F%E7%BB%84%E7%AC%AC%E4%BA%8C%E6%89%B9%E6%88%90%E6%9E%9C%E9%9B%86%E6%88%90%E8%B5%9E%E5%8A%A9%E9%83%A8%E5%88%86/jiang/%E4%B8%AD%E5%9B%BD%E7%A4%BE%E4%BC%9A%E4%B8%BB%E4%B9%89%E6%97%B6%E4%BB%A3%E7%9A%84%E6%95%99%E8%82%B2%E9%9D%A9%E5%91%BD%20%E7%A0%94%E7%A9%B6%E8%B5%84%E6%96%99%E9%9B%86%E6%88%90pdf%EF%BC%88%E8%BF%91800%E7%A7%8D%EF%BC%89/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%A4%A7%E5%AD%A6%E7%94%9F/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%AD%A6%E5%91%98%E7%9A%84%E4%BC%98%E7%A7%80%E4%BB%A3%E8%A1%A8----%E7%BA%AA%E5%B0%8F%E9%BE%99%E6%95%99%E6%8E%88.txt",
        "reference_sha256": "694df25c2142e13cd79b460719e457916eda69a79c64d150d9ab2b7961772302",
        "reference_kind": "original-source-binary-marker-audit",
    },
    "mlmrl-vomebook-gongnongbing-11-marker-v1": {
        "source_sha256": "25b387a9dbc3c54758e3f1877f50f77a2b12c23493ad448e44cd8bb536f14bba",
        "mode": "legacy-marker-strip",
        "source_encoding": "gb18030",
        "marker_hex": "f8fa0702cb35f501",
        "marker_count": 15,
        "marker_offsets": [1025, 2057, 3089, 4121, 5153, 6185, 7217, 8249, 9281, 10313, 11345, 12377, 13409, 14441, 15473],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/VOMEBOOK/blob/7f338fb4af738ccd6e8353b90db9c16023497555/%E9%A9%AC%E5%88%97%E4%B9%8B%E5%A3%B0ebook%E5%B0%8F%E7%BB%84%E7%AC%AC%E4%BA%8C%E6%89%B9%E6%88%90%E6%9E%9C%E9%9B%86%E6%88%90%E8%B5%9E%E5%8A%A9%E9%83%A8%E5%88%86/jiang/%E4%B8%AD%E5%9B%BD%E7%A4%BE%E4%BC%9A%E4%B8%BB%E4%B9%89%E6%97%B6%E4%BB%A3%E7%9A%84%E6%95%99%E8%82%B2%E9%9D%A9%E5%91%BD%20%E7%A0%94%E7%A9%B6%E8%B5%84%E6%96%99%E9%9B%86%E6%88%90pdf%EF%BC%88%E8%BF%91800%E7%A7%8D%EF%BC%89/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%A4%A7%E5%AD%A6%E7%94%9F/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%AD%A6%E5%91%98%E7%9A%84%E4%BC%98%E7%A7%80%E4%BB%A3%E8%A1%A8----%E9%82%93%E6%96%87%E5%AE%BD%E9%99%A2%E5%A3%AB.txt",
        "reference_sha256": "25b387a9dbc3c54758e3f1877f50f77a2b12c23493ad448e44cd8bb536f14bba",
        "reference_kind": "original-source-binary-marker-audit",
    },
    "mlmrl-vomebook-gongnongbing-12-marker-v1": {
        "source_sha256": "a147021796a7eb084844d6be7e958e7c4ec78cffb5b2464baa043b05e1280192",
        "mode": "legacy-marker-strip",
        "source_encoding": "gb18030",
        "marker_hex": "f8fa0702cb35f501",
        "marker_count": 1,
        "marker_offsets": [1025],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/VOMEBOOK/blob/7f338fb4af738ccd6e8353b90db9c16023497555/%E9%A9%AC%E5%88%97%E4%B9%8B%E5%A3%B0ebook%E5%B0%8F%E7%BB%84%E7%AC%AC%E4%BA%8C%E6%89%B9%E6%88%90%E6%9E%9C%E9%9B%86%E6%88%90%E8%B5%9E%E5%8A%A9%E9%83%A8%E5%88%86/jiang/%E4%B8%AD%E5%9B%BD%E7%A4%BE%E4%BC%9A%E4%B8%BB%E4%B9%89%E6%97%B6%E4%BB%A3%E7%9A%84%E6%95%99%E8%82%B2%E9%9D%A9%E5%91%BD%20%E7%A0%94%E7%A9%B6%E8%B5%84%E6%96%99%E9%9B%86%E6%88%90pdf%EF%BC%88%E8%BF%91800%E7%A7%8D%EF%BC%89/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%A4%A7%E5%AD%A6%E7%94%9F/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%AD%A6%E5%91%98%E7%9A%84%E4%BC%98%E7%A7%80%E4%BB%A3%E8%A1%A8----%E9%99%88%E8%B5%9B%E5%A8%9F%E9%99%A2%E5%A3%AB.txt",
        "reference_sha256": "a147021796a7eb084844d6be7e958e7c4ec78cffb5b2464baa043b05e1280192",
        "reference_kind": "original-source-binary-marker-audit",
    },
    "mlmrl-vomebook-gongnongbing-13-marker-v1": {
        "source_sha256": "0a1e6651853877c4aa225b7d76fab862222f68f1a5c6c84c6476ab8a17c5d156",
        "mode": "legacy-marker-strip",
        "source_encoding": "gb18030",
        "marker_hex": "f8fa0702cb35f501",
        "marker_count": 3,
        "marker_offsets": [1025, 2057, 3089],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/VOMEBOOK/blob/7f338fb4af738ccd6e8353b90db9c16023497555/%E9%A9%AC%E5%88%97%E4%B9%8B%E5%A3%B0ebook%E5%B0%8F%E7%BB%84%E7%AC%AC%E4%BA%8C%E6%89%B9%E6%88%90%E6%9E%9C%E9%9B%86%E6%88%90%E8%B5%9E%E5%8A%A9%E9%83%A8%E5%88%86/jiang/%E4%B8%AD%E5%9B%BD%E7%A4%BE%E4%BC%9A%E4%B8%BB%E4%B9%89%E6%97%B6%E4%BB%A3%E7%9A%84%E6%95%99%E8%82%B2%E9%9D%A9%E5%91%BD%20%E7%A0%94%E7%A9%B6%E8%B5%84%E6%96%99%E9%9B%86%E6%88%90pdf%EF%BC%88%E8%BF%91800%E7%A7%8D%EF%BC%89/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%A4%A7%E5%AD%A6%E7%94%9F/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%AD%A6%E5%91%98%E7%9A%84%E4%BC%98%E7%A7%80%E4%BB%A3%E8%A1%A8----%E9%9F%A9%E5%BE%B7%E6%B0%91%E6%95%99%E6%8E%88.txt",
        "reference_sha256": "0a1e6651853877c4aa225b7d76fab862222f68f1a5c6c84c6476ab8a17c5d156",
        "reference_kind": "original-source-binary-marker-audit",
    },
    "mlmrl-vomebook-gongnongbing-14-marker-v1": {
        "source_sha256": "acb250d436746b1ab8c36691b153818099dc5920637503491d37145db6c1ed3a",
        "mode": "legacy-marker-strip",
        "source_encoding": "gb18030",
        "marker_hex": "f8fa0702cb35f501",
        "marker_count": 2,
        "marker_offsets": [1025, 2057],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/VOMEBOOK/blob/7f338fb4af738ccd6e8353b90db9c16023497555/%E9%A9%AC%E5%88%97%E4%B9%8B%E5%A3%B0ebook%E5%B0%8F%E7%BB%84%E7%AC%AC%E4%BA%8C%E6%89%B9%E6%88%90%E6%9E%9C%E9%9B%86%E6%88%90%E8%B5%9E%E5%8A%A9%E9%83%A8%E5%88%86/jiang/%E4%B8%AD%E5%9B%BD%E7%A4%BE%E4%BC%9A%E4%B8%BB%E4%B9%89%E6%97%B6%E4%BB%A3%E7%9A%84%E6%95%99%E8%82%B2%E9%9D%A9%E5%91%BD%20%E7%A0%94%E7%A9%B6%E8%B5%84%E6%96%99%E9%9B%86%E6%88%90pdf%EF%BC%88%E8%BF%91800%E7%A7%8D%EF%BC%89/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%A4%A7%E5%AD%A6%E7%94%9F/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%AD%A6%E5%91%98%E7%9A%84%E4%BC%98%E7%A7%80%E4%BB%A3%E8%A1%A8----%E9%BB%84%E5%A5%87%E5%B8%86.txt",
        "reference_sha256": "acb250d436746b1ab8c36691b153818099dc5920637503491d37145db6c1ed3a",
        "reference_kind": "original-source-binary-marker-audit",
    },
    "mlmrl-vomebook-gongnongbing-15-marker-v1": {
        "source_sha256": "0131979d3baaf2a783b8888ba7f12797220f043f6d4bb30e2525b3724dabe4e3",
        "mode": "legacy-marker-strip",
        "source_encoding": "gb18030",
        "marker_hex": "f8fa0702cb35f501",
        "marker_count": 3,
        "marker_offsets": [1025, 2057, 3089],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/VOMEBOOK/blob/7f338fb4af738ccd6e8353b90db9c16023497555/%E9%A9%AC%E5%88%97%E4%B9%8B%E5%A3%B0ebook%E5%B0%8F%E7%BB%84%E7%AC%AC%E4%BA%8C%E6%89%B9%E6%88%90%E6%9E%9C%E9%9B%86%E6%88%90%E8%B5%9E%E5%8A%A9%E9%83%A8%E5%88%86/jiang/%E4%B8%AD%E5%9B%BD%E7%A4%BE%E4%BC%9A%E4%B8%BB%E4%B9%89%E6%97%B6%E4%BB%A3%E7%9A%84%E6%95%99%E8%82%B2%E9%9D%A9%E5%91%BD%20%E7%A0%94%E7%A9%B6%E8%B5%84%E6%96%99%E9%9B%86%E6%88%90pdf%EF%BC%88%E8%BF%91800%E7%A7%8D%EF%BC%89/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%A4%A7%E5%AD%A6%E7%94%9F/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%AD%A6%E5%91%98%E7%9A%84%E4%BC%98%E7%A7%80%E4%BB%A3%E8%A1%A8%EF%BC%8C%E7%A7%91%E5%AD%A6%E7%BD%91%E5%8D%9A%E4%B8%BB----%E4%BD%95%E8%A3%95%E6%B0%91%E6%95%99%E6%8E%88.txt",
        "reference_sha256": "0131979d3baaf2a783b8888ba7f12797220f043f6d4bb30e2525b3724dabe4e3",
        "reference_kind": "original-source-binary-marker-audit",
    },
    "mlmrl-vomebook-gongnongbing-16-marker-v1": {
        "source_sha256": "d01011b638e17ef423aca80968277fcba133538bc955b833d23b43e504c6bd6e",
        "mode": "legacy-marker-strip",
        "source_encoding": "gb18030",
        "marker_hex": "f8fa0702cb35f501",
        "marker_count": 28,
        "marker_offsets": [1025, 2057, 3089, 4121, 5153, 6185, 7217, 8249, 9281, 10313, 11345, 12377, 13409, 14441, 15473, 16505, 17537, 18569, 19601, 20633, 21665, 22697, 23729, 24761, 25793, 26825, 27857, 28889],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/VOMEBOOK/blob/7f338fb4af738ccd6e8353b90db9c16023497555/%E9%A9%AC%E5%88%97%E4%B9%8B%E5%A3%B0ebook%E5%B0%8F%E7%BB%84%E7%AC%AC%E4%BA%8C%E6%89%B9%E6%88%90%E6%9E%9C%E9%9B%86%E6%88%90%E8%B5%9E%E5%8A%A9%E9%83%A8%E5%88%86/jiang/%E4%B8%AD%E5%9B%BD%E7%A4%BE%E4%BC%9A%E4%B8%BB%E4%B9%89%E6%97%B6%E4%BB%A3%E7%9A%84%E6%95%99%E8%82%B2%E9%9D%A9%E5%91%BD%20%E7%A0%94%E7%A9%B6%E8%B5%84%E6%96%99%E9%9B%86%E6%88%90pdf%EF%BC%88%E8%BF%91800%E7%A7%8D%EF%BC%89/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%A4%A7%E5%AD%A6%E7%94%9F/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%AD%A6%E5%91%98%E9%AB%98%E9%BB%98%E6%B3%A2%EF%BC%9A%E6%96%87%E9%9D%A9%E5%8D%81%E5%B9%B4%E6%98%AF%E9%BB%84%E9%87%91%E6%97%B6%E6%9C%9F.txt",
        "reference_sha256": "d01011b638e17ef423aca80968277fcba133538bc955b833d23b43e504c6bd6e",
        "reference_kind": "original-source-binary-marker-audit",
    },
    "mlmrl-vomebook-gongnongbing-17-marker-v1": {
        "source_sha256": "5b857ec40715e55a451cedf59bbd08b0d14ef1bc46c1226cc3447db8ba653a12",
        "mode": "legacy-marker-strip",
        "source_encoding": "gb18030",
        "marker_hex": "f8fa0702cb35f501",
        "marker_count": 1,
        "marker_offsets": [1025],
        "reference_url": "https://huggingface.co/datasets/VoiceOfML/VOMEBOOK/blob/7f338fb4af738ccd6e8353b90db9c16023497555/%E9%A9%AC%E5%88%97%E4%B9%8B%E5%A3%B0ebook%E5%B0%8F%E7%BB%84%E7%AC%AC%E4%BA%8C%E6%89%B9%E6%88%90%E6%9E%9C%E9%9B%86%E6%88%90%E8%B5%9E%E5%8A%A9%E9%83%A8%E5%88%86/jiang/%E4%B8%AD%E5%9B%BD%E7%A4%BE%E4%BC%9A%E4%B8%BB%E4%B9%89%E6%97%B6%E4%BB%A3%E7%9A%84%E6%95%99%E8%82%B2%E9%9D%A9%E5%91%BD%20%E7%A0%94%E7%A9%B6%E8%B5%84%E6%96%99%E9%9B%86%E6%88%90pdf%EF%BC%88%E8%BF%91800%E7%A7%8D%EF%BC%89/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%A4%A7%E5%AD%A6%E7%94%9F/%E5%B7%A5%E5%86%9C%E5%85%B5%E5%AD%A6%E5%91%98%E9%AD%8F%E6%89%BF%E6%80%9D.txt",
        "reference_sha256": "5b857ec40715e55a451cedf59bbd08b0d14ef1bc46c1226cc3447db8ba653a12",
        "reference_kind": "original-source-binary-marker-audit",
    },
}


class TextEncodingError(ValueError):
    pass


def validate_text(text: str, *, preserve_controls: dict | None = None) -> str:
    approved = {} if preserve_controls is None else preserve_controls
    if (not isinstance(approved, dict) or any(key != "U+001A" or type(count) is not int or count < 1
                                             for key, count in approved.items())):
        raise TextEncodingError("only exact reviewed U+001A counts may be preserved")
    controls = Counter(f"U+{ord(c):04X}" for c in text if ord(c) < 32 and c not in "\t\n\r\f")
    if controls != approved:
        raise TextEncodingError("text contains NUL or binary control bytes")
    # Encoding is strict: no surrogate replacement, whitespace cleanup, newline
    # normalization or removal of a literal Unicode replacement character.
    text.encode("utf-8", errors="strict")
    return text


def decode_windows_936(raw: bytes) -> str:
    """Strict Python GBK plus Windows' single-byte 0x80 euro, never replacement.

    A GBK trailing 0x80 belongs to its two-byte character, not to the euro mapping.
    All chunks still use the strict codec and the complete result must round-trip.
    Undefined CP936 extensions remain errors rather than guessed characters.
    """
    parts, start, cursor = [], 0, 0
    while cursor < len(raw):
        byte = raw[cursor]
        if byte == 0x80:
            parts.extend((raw[start:cursor].decode("gbk", errors="strict"), "€"))
            cursor += 1
            start = cursor
        else:
            cursor += 2 if 0x81 <= byte <= 0xfe else 1
    parts.append(raw[start:].decode("gbk", errors="strict"))
    text = "".join(parts)
    if b"\x80".join(part.encode("gbk", errors="strict") for part in text.split("€")) != raw:
        raise TextEncodingError("Windows-936 source does not round-trip")
    return text


def decode_source_text(raw: bytes, *, hint: str = "", encoding: str | None = None,
                       preserve_controls: dict | None = None) -> tuple[str, str, str]:
    if looks_like_lfs_pointer(raw):
        raise TextEncodingError("source is a Git LFS pointer, not document text")
    if preserve_controls is not None and not encoding:
        raise TextEncodingError("reviewed controls require an explicit encoding")
    if encoding:
        if encoding.lower().replace("_", "-") == "windows-936":
            return validate_text(decode_windows_936(raw), preserve_controls=preserve_controls), "windows-936", "explicit"
        try:
            canonical = codecs.lookup(encoding).name
        except LookupError as error:
            raise TextEncodingError("unknown explicit text encoding") from error
        allowed = {*STRICT_ENCODINGS, "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be",
                   "utf-32", "utf-32-le", "utf-32-be", "cp1251", "cp1252", "iso8859-1"}
        if canonical not in {codecs.lookup(name).name for name in allowed}:
            raise TextEncodingError("unsupported explicit text encoding")
        if canonical == "utf-8" and raw.startswith(UTF8_BOM):
            canonical = "utf-8-sig"
        text = validate_text(raw.decode(canonical, errors="strict"), preserve_controls=preserve_controls)
        if preserve_controls and text.encode(canonical, errors="strict") != raw:
            raise TextEncodingError("reviewed-control source does not round-trip")
        return text, canonical, "explicit"
    for signature, codec in ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
                             (codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"),
                             (codecs.BOM_UTF16_BE, "utf-16")):
        if raw.startswith(signature):
            return validate_text(raw.decode(codec, errors="strict")), codec, "bom"
    even, odd = raw[::2].count(0), raw[1::2].count(0)
    if odd > len(raw) / 8 and odd > even * 4:
        return validate_text(raw.decode("utf-16-le", errors="strict")), "utf-16-le", "nul-parity"
    if even > len(raw) / 8 and even > odd * 4:
        return validate_text(raw.decode("utf-16-be", errors="strict")), "utf-16-be", "nul-parity"
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        pass
    else:
        return validate_text(text), "utf-8", "strict-utf8"
    if re.search(r"[\u0400-\u04ff]", hint):
        text = raw.decode("cp1251", errors="strict")
        if text.encode("cp1251") != raw:
            raise TextEncodingError("legacy source does not round-trip")
        return validate_text(text), "cp1251", "cyrillic-path"
    candidates = {}
    probe = raw[:65536]
    for codec in STRICT_ENCODINGS:
        if codec == "utf-8":
            continue
        try:
            decoder = codecs.getincrementaldecoder(codec)(errors="strict")
            text = validate_text(decoder.decode(probe, final=len(probe) == len(raw)))
            pending = decoder.getstate()[0]
            if text.encode(codec, errors="strict") + pending != probe:
                continue
        except (UnicodeError, TextEncodingError):
            continue
        sample = text[:20000]
        score = text_quality_score(text) + sum(c in COMMON_CJK for c in sample) * 5
        # GBK/GB18030 aliases producing identical text are one interpretation.
        candidates.setdefault(text, (score, codec))
    ranked = sorted(((score, codec, text) for text, (score, codec) in candidates.items()), reverse=True)
    if not ranked or ranked[0][0] <= 0:
        raise TextEncodingError("no strict legacy decoding; provide an encoding override")
    score, codec, text = ranked[0]
    if len(ranked) > 1 and score - ranked[1][0] <= max(4, len(text[:20000]) * .02):
        raise TextEncodingError("ambiguous legacy encoding; provide an encoding override")
    text = validate_text(raw.decode(codec, errors="strict"))
    if text.encode(codec, errors="strict") != raw:
        raise TextEncodingError("legacy source does not round-trip")
    return text, codec, "legacy-score"


def normalize_utf8(raw: bytes, *, hint: str = "", encoding: str | None = None,
                    preserve_controls: dict | None = None,
                    remove_prefix_byte: dict | None = None,
                    reviewed_source_repair: str | None = None) -> tuple[bytes, dict]:
    original = raw
    if reviewed_source_repair is not None:
        if (not encoding or remove_prefix_byte is not None or preserve_controls is not None
                or not isinstance(reviewed_source_repair, str) or reviewed_source_repair not in REVIEWED_SOURCE_REPAIRS):
            raise TextEncodingError("source repair requires one known reviewed recipe, explicit UTF-8 and no other repair")
        try:
            codec = codecs.lookup(encoding).name
        except (LookupError, TypeError) as error:
            raise TextEncodingError("unknown explicit text encoding") from error
        repair = REVIEWED_SOURCE_REPAIRS[reviewed_source_repair]
        if hashlib.sha256(raw).hexdigest() != repair["source_sha256"]:
            raise TextEncodingError("source differs from the complete approved repair SHA-256")
        if repair.get("mode") == "control-strip":
            # Only invisible control artifacts at exact reviewed character
            # offsets are removed. All other text and the complete source
            # encoding round-trip are verified.
            source_encoding_name = repair.get("source_encoding")
            if not isinstance(source_encoding_name, str):
                raise TextEncodingError("reviewed control strip has no approved source encoding")
            try:
                expected_codec = codecs.lookup(source_encoding_name).name
            except LookupError as error:
                raise TextEncodingError("reviewed control strip source encoding is unknown") from error
            if expected_codec not in {"utf-8", "gb18030"} or codec != expected_codec:
                raise TextEncodingError("reviewed control strip encoding differs from the approved recipe")
            try:
                source_text = raw.decode(codec, errors="strict")
            except UnicodeDecodeError as error:
                raise TextEncodingError("reviewed control strip source is not strict text") from error
            if source_text.encode(codec, errors="strict") != raw:
                raise TextEncodingError("reviewed control strip source does not round-trip")
            offsets = repair.get("control_offsets")
            codepoints = repair.get("control_codepoints")
            if (not isinstance(offsets, list) or not offsets
                    or any(type(offset) is not int for offset in offsets)
                    or offsets != sorted(offsets) or len(set(offsets)) != len(offsets)
                    or not isinstance(codepoints, list) or len(codepoints) != len(offsets)):
                raise TextEncodingError("reviewed control offsets are invalid")
            for offset, codepoint in zip(offsets, codepoints):
                if (not isinstance(codepoint, str) or len(codepoint) != 1
                        or ord(codepoint) >= 32 or codepoint in "\t\n\r\f"
                        or offset < 0 or offset >= len(source_text)
                        or source_text[offset] != codepoint):
                    raise TextEncodingError("reviewed control differs from the approved offset")
            drop = set(offsets)
            text = validate_text("".join(char for index, char in enumerate(source_text) if index not in drop))
            pieces, previous = [], 0
            for offset, codepoint in zip(offsets, codepoints):
                pieces.extend((source_text[previous:offset], codepoint))
                previous = offset + 1
            pieces.append(source_text[previous:])
            reconstructed = "".join(pieces)
            if reconstructed != source_text or reconstructed.encode(codec, errors="strict") != raw:
                raise TextEncodingError("reviewed control removal does not reconstruct the complete original")
            normalized = UTF8_BOM + text.encode("utf-8", errors="strict")
            if normalized.decode("utf-8-sig", errors="strict") != text:
                raise TextEncodingError("UTF-8 verification failed")
            metadata = {
                "encoding": "utf-8", "bom": True, "profile": PROFILE,
                "source_encoding": codec, "encoding_decision": "explicit",
                "characters": len(text), "reviewed_source_repair": reviewed_source_repair,
                "source_roundtrip_verified": True,
                "normalization_recipe": "strict-reviewed-control-strip-v1",
                "replaced_source_span": {
                    "control_codepoints": codepoints,
                    "control_offsets": offsets,
                    "removed_controls": len(offsets),
                    "original_characters": len(source_text),
                    "original_bytes": len(raw),
                    "replacement_bytes": len(text.encode("utf-8")),
                    "reference_url": repair["reference_url"],
                    "reference_sha256": repair["reference_sha256"],
                    "reference_kind": repair.get("reference_kind", ""),
                },
            }
            return normalized, metadata
        if repair.get("mode") == "legacy-marker-strip":
            # A fixed binary marker was inserted into an otherwise complete
            # legacy text. Only the exact reviewed marker occurrences are
            # removed; the cleaned bytes must strict-decode and round-trip.
            source_encoding_name = repair.get("source_encoding")
            if not isinstance(source_encoding_name, str):
                raise TextEncodingError("reviewed marker strip has no approved source encoding")
            try:
                expected_codec = codecs.lookup(source_encoding_name).name
            except LookupError as error:
                raise TextEncodingError("reviewed marker strip source encoding is unknown") from error
            if expected_codec != "gb18030" or codec != expected_codec:
                raise TextEncodingError("reviewed marker strip encoding differs from the approved recipe")
            marker_hex = repair.get("marker_hex")
            if not isinstance(marker_hex, str) or not re.fullmatch(r"(?:[0-9a-f]{2})+", marker_hex):
                raise TextEncodingError("reviewed binary marker is invalid")
            marker = bytes.fromhex(marker_hex)
            offsets = repair.get("marker_offsets")
            if (not isinstance(offsets, list) or not offsets
                    or any(type(offset) is not int for offset in offsets)
                    or offsets != sorted(offsets) or len(set(offsets)) != len(offsets)
                    or repair.get("marker_count") != len(offsets)):
                raise TextEncodingError("reviewed binary marker offsets are invalid")
            for offset in offsets:
                if offset < 0 or offset + len(marker) > len(raw) or raw[offset:offset + len(marker)] != marker:
                    raise TextEncodingError("reviewed binary marker differs from the approved offset")
            if any(left + len(marker) > right for left, right in zip(offsets, offsets[1:])):
                raise TextEncodingError("reviewed binary marker offsets overlap")
            pieces, previous = [], 0
            for offset in offsets:
                pieces.append(raw[previous:offset])
                previous = offset + len(marker)
            pieces.append(raw[previous:])
            clean = b"".join(pieces)
            if raw.count(marker) != len(offsets) or len(clean) != len(raw) - len(marker) * len(offsets):
                raise TextEncodingError("reviewed binary marker count differs from the approved recipe")
            try:
                text = validate_text(clean.decode("gb18030", errors="strict"))
            except UnicodeDecodeError as error:
                raise TextEncodingError("reviewed marker strip does not leave strict GB18030 text") from error
            if text.encode("gb18030", errors="strict") != clean:
                raise TextEncodingError("reviewed marker strip cleaned source does not round-trip")
            normalized = UTF8_BOM + text.encode("utf-8", errors="strict")
            if normalized.decode("utf-8-sig", errors="strict") != text:
                raise TextEncodingError("UTF-8 verification failed")
            metadata = {
                "encoding": "utf-8", "bom": True, "profile": PROFILE,
                "source_encoding": "gb18030", "encoding_decision": "explicit",
                "characters": len(text), "reviewed_source_repair": reviewed_source_repair,
                "source_roundtrip_verified": True,
                "normalization_recipe": "strict-legacy-binary-marker-strip-v1",
                "replaced_source_span": {
                    "marker_hex": marker_hex,
                    "marker_count": len(offsets),
                    "marker_offsets": offsets,
                    "original_bytes": len(raw),
                    "cleaned_bytes": len(clean),
                    "replacement_bytes": len(text.encode("utf-8")),
                    "reference_url": repair["reference_url"],
                    "reference_sha256": repair["reference_sha256"],
                    "reference_kind": repair.get("reference_kind", ""),
                },
            }
            return normalized, metadata
        if codec not in {"utf-8", "utf-8-sig"}:
            raise TextEncodingError("reviewed source repair is limited to strict UTF-8 originals")
        if repair.get("mode") == "suffix-strip":
            # The reviewed tail is non-text image data decoded as text. The
            # replacement retains only the caption sections between images.
            if codec != "utf-8" or raw.startswith(UTF8_BOM):
                raise TextEncodingError("reviewed suffix strip requires strict UTF-8 without a BOM")
            try:
                source_text = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise TextEncodingError("reviewed suffix strip requires strict UTF-8") from error
            offset_chars = repair.get("offset_chars")
            if type(offset_chars) is not int or not 0 <= offset_chars <= len(source_text):
                raise TextEncodingError("reviewed suffix offset is invalid")
            suffix = source_text[offset_chars:]
            if (len(suffix) != repair.get("original_suffix_characters")
                    or hashlib.sha256(suffix.encode("utf-8")).hexdigest() != repair.get("original_suffix_sha256")):
                raise TextEncodingError("reviewed source suffix differs from the approved span")
            replacement_source = repair.get("replacement_text")
            if not isinstance(replacement_source, str):
                raise TextEncodingError("reviewed suffix replacement is invalid")
            replacement_text = validate_text(replacement_source)
            if hashlib.sha256(replacement_text.encode("utf-8")).hexdigest() != repair.get("replacement_sha256"):
                raise TextEncodingError("reviewed suffix replacement differs from the approved SHA-256")
            text = validate_text(source_text[:offset_chars] + replacement_text)
            normalized = UTF8_BOM + text.encode("utf-8", errors="strict")
            if normalized.decode("utf-8-sig", errors="strict") != text:
                raise TextEncodingError("UTF-8 verification failed")
            metadata = {
                "encoding": "utf-8", "bom": True, "profile": PROFILE,
                "source_encoding": "utf-8", "encoding_decision": "explicit",
                "characters": len(text), "reviewed_source_repair": reviewed_source_repair,
                "source_roundtrip_verified": True,
                "normalization_recipe": "strict-utf8-reviewed-source-suffix-strip-v1",
                "replaced_source_span": {
                    "offset_chars": offset_chars,
                    "original_characters": len(suffix),
                    "original_bytes": len(suffix.encode("utf-8")),
                    "original_suffix_sha256": repair["original_suffix_sha256"],
                    "replacement_bytes": len(replacement_text.encode("utf-8")),
                    "replacement_text": replacement_text,
                    "image_spans": repair.get("image_spans", []),
                    "reference_url": repair["reference_url"],
                    "reference_sha256": repair["reference_sha256"],
                    "reference_kind": repair.get("reference_kind", ""),
                },
            }
            return normalized, metadata
        raw.decode("utf-8", errors="strict")
        offset = repair["offset"]
        old_span = repair["original_text"].encode("utf-8", errors="strict")
        new_span = validate_text(repair["replacement_text"]).encode("utf-8", errors="strict")
        if raw[offset:offset + len(old_span)] != old_span:
            raise TextEncodingError("reviewed source span differs from original bytes")
        raw = raw[:offset] + new_span + raw[offset + len(old_span):]
    if remove_prefix_byte is not None:
        removal = remove_prefix_byte
        if (not encoding or not isinstance(removal, dict) or set(removal) != {"offset", "hex"}
                or type(removal["offset"]) is not int or not 0 <= removal["offset"] < min(4096, len(raw))
                or not isinstance(removal["hex"], str) or not re.fullmatch(r"[0-9a-f]{2}", removal["hex"])):
            raise TextEncodingError("prefix-byte repair needs explicit encoding and one exact reviewed byte within 4096 bytes")
        offset = removal["offset"]
        if raw[offset:offset + 1].hex() != removal["hex"]:
            raise TextEncodingError("reviewed prefix byte does not match source")
        raw = raw[:offset] + raw[offset + 1:]
    text, source_encoding, decision = decode_source_text(raw, hint=hint, encoding=encoding,
                                                       preserve_controls=preserve_controls)
    normalized = UTF8_BOM + text.encode("utf-8", errors="strict")
    if normalized.decode("utf-8-sig", errors="strict") != text:
        raise TextEncodingError("UTF-8 verification failed")
    metadata = {"encoding": "utf-8", "bom": True, "profile": PROFILE,
                "source_encoding": source_encoding, "encoding_decision": decision,
                "characters": len(text)}
    if preserve_controls:
        metadata["preserved_controls"] = dict(preserve_controls)
    if remove_prefix_byte is not None:
        retained = (b"\x80".join(part.encode("gbk", errors="strict") for part in text.split("€"))
                    if source_encoding == "windows-936" else text.encode(source_encoding, errors="strict"))
        if retained != raw or retained[:offset] + bytes.fromhex(removal["hex"]) + retained[offset:] != original:
            raise TextEncodingError("reviewed prefix repair does not reconstruct the complete original")
        metadata["removed_prefix_byte"] = dict(removal)
        metadata["normalization_recipe"] = "strict-decode-with-reviewed-prefix-byte-removal-v1"
    if reviewed_source_repair is not None:
        retained = text.encode(source_encoding, errors="strict")
        reconstructed = retained[:offset] + old_span + retained[offset + len(new_span):]
        if retained != raw or reconstructed != original:
            raise TextEncodingError("reviewed source repair does not reconstruct the complete original")
        metadata["reviewed_source_repair"] = reviewed_source_repair
        metadata["replaced_source_span"] = {
            "offset": offset, "original_bytes": len(old_span), "replacement_bytes": len(new_span),
            "original_text": repair["original_text"], "replacement_text": repair["replacement_text"],
            "reference_url": repair["reference_url"], "reference_sha256": repair["reference_sha256"],
        }
        metadata["normalization_recipe"] = "strict-utf8-reviewed-source-repair-v1"
    if source_encoding == "windows-936" or preserve_controls or remove_prefix_byte is not None or reviewed_source_repair is not None:
        metadata["source_roundtrip_verified"] = True
    return normalized, metadata
