from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image,ImageDraw
from scripts import ocr_latin_spacing as spacing


class LatinSpacingTests(unittest.TestCase):
    def test_word_gaps_need_two_readings_and_actual_blank_pixels(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'eng.traineddata').write_bytes(b'test-model')
            image=Image.new('RGB',(400,50),'white');draw=ImageDraw.Draw(image)
            for first in [10,40,70,100,130,230,260,290,320]:
                draw.rectangle((first,10,first+15,35),fill='black')
            path=root/'image.png';image.save(path)
            blocks=[{'t':'CLARENDONPRESS','b':[0,0,1,1],'c':.99,'s':'ocr'}]
            def run(command,**kwargs):
                scale=int(Path(command[1]).stem.rsplit('-',1)[1])
                lines=['level\tleft\ttop\twidth\theight\tconf\ttext']
                for text,a,b in [('CLARENDON',10,146),('PRESS',230,336)]:
                    lines.append(f'5\t{(a+10)*scale}\t{20*scale}\t{(b-a)*scale}\t{25*scale}\t95\t{text}')
                return SimpleNamespace(returncode=0,stdout='\n'.join(lines),stderr='')
            with patch.dict('os.environ',{'OCR_SCRIPT_MODEL_DIR':temp}),patch.object(spacing.subprocess,'run',side_effect=run):
                output,proof=spacing.recognize(path,blocks)
            self.assertEqual(output[0]['t'],'CLARENDON PRESS')
            self.assertEqual(blocks[0]['t'],'CLARENDONPRESS')
            self.assertTrue(proof['regions'][0]['accepted'])
            self.assertEqual(len(proof['regions'][0]['attempts']),2)
            with patch.dict('os.environ',{'OCR_SCRIPT_MODEL_DIR':temp}),patch.object(spacing.subprocess,'run',side_effect=subprocess_error()):
                output,proof=spacing.recognize(path,blocks)
            self.assertEqual(output,blocks)
            self.assertFalse(proof['regions'][0]['accepted'])


def subprocess_error():
    return OSError('test: independent recognizer unavailable')
