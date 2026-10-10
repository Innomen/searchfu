"""Private local progressive evidence; never a remotely readable diagnostic."""
import json
import os
from pathlib import Path
import uuid
from jobs import _write


class EvidenceBank:
    def __init__(self,role):
        if role not in ('gui','usb-comparison'):raise ValueError('invalid_evidence_origin')
        base=Path(os.environ['SEARCHFU_METRICS_DB']).parent/'evidence' if os.environ.get('SEARCHFU_METRICS_DB') else Path(os.environ.get('XDG_STATE_HOME',str(Path.home()/'.local/state')))/'searchfu/evidence'
        if base.resolve().is_relative_to(Path(__file__).resolve().parent):raise ValueError('evidence_must_live_outside_source')
        base.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=base/uuid.uuid4().hex;self.path.mkdir(mode=0o700)
        _write(self.path/'origin.json',{'role':role})
    def append(self,event):
        data=(json.dumps(event)+'\n').encode()
        fd=os.open(self.path/'events.jsonl',os.O_CREAT|os.O_WRONLY|os.O_APPEND,0o600)
        try:
            with os.fdopen(fd,'wb') as output:output.write(data);output.flush()
        except Exception:raise
        _write(self.path/'latest.json',event)
