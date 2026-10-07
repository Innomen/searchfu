import os,unittest
from unittest.mock import Mock,patch
from search import Models

class EncodeBatchTests(unittest.TestCase):
    def test_device_defaults_and_explicit_override(self):
        for cuda,override,expected in [("0",None,8),("1",None,64),("1","32",32)]:
            model=Models();model._text=Mock()
            with patch.dict(os.environ,{"SEARCHFU_CUDA":cuda},clear=True):
                if override:os.environ["SEARCHFU_ENCODE_BATCH"]=override
                model.text_vecs(["Synthetic fixture"])
            self.assertEqual(model._text.encode.call_args.kwargs["batch_size"],expected)
    def test_invalid_batches_do_not_encode(self):
        for value in ("0","1025","invalid"):
            model=Models();model._text=Mock()
            with patch.dict(os.environ,{"SEARCHFU_ENCODE_BATCH":value}):
                with self.assertRaises(ValueError):model.text_vecs(["Synthetic fixture"])
            model._text.encode.assert_not_called()
