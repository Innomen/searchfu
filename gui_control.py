"""Same-user desktop control; replies never contain query or result payloads."""
from pathlib import Path
import json, os, socket, sys

def socket_path():
    p=Path(os.environ.get('SEARCHFU_GUI_SOCKET',str(Path(os.environ.get('XDG_STATE_HOME',str(Path.home()/'.local/state')))/'searchfu/gui.sock')))
    if p.resolve().is_relative_to(Path(__file__).resolve().parent):raise ValueError('invalid_control_location')
    return p

def request(command,timeout=3):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
        s.settimeout(timeout);s.connect(str(socket_path()));s.sendall(json.dumps(command).encode()+b'\n')
        raw=b''
        while b'\n' not in raw:
            part=s.recv(4096)
            if not part:raise RuntimeError('control_disconnected')
            raw+=part
            if len(raw)>16384:raise RuntimeError('invalid_control_reply')
        reply=json.loads(raw.split(b'\n')[0])
        # Defense in depth: only this closed envelope may leave the local client.
        allowed={'accepted','running','comparison_running','phase','outcome','result_count','elapsed_seconds','reload_pending','error'}
        if set(reply)-allowed:raise RuntimeError('invalid_control_reply')
        if 'phase' in reply and reply['phase'] not in {'ready','starting','opening','keyword_scan','filename_scan','keyword','encoding','candidate','candidates','semantic','quantized','full_precision','refine','refined','rerank','reranked','reranked_early','refresh','done','cancelled','deep','working','cancelling','failed'}:raise RuntimeError('invalid_control_reply')
        if reply.get('error') not in (None,'busy','invalid_command','unavailable'):raise RuntimeError('invalid_control_reply')
        if reply.get('outcome') not in (None,'done','cancelled','failed','primary_timeout'):raise RuntimeError('invalid_control_reply')
        for key in ('accepted','running','comparison_running','reload_pending'):
            if key in reply and type(reply[key]) is not bool:raise RuntimeError('invalid_control_reply')
        for key in ('result_count','elapsed_seconds'):
            if key in reply and (type(reply[key]) not in (int,float) or not 0<=reply[key]<=1e9):raise RuntimeError('invalid_control_reply')
        return reply

def install(window,app):
    import fcntl
    from PySide6.QtNetwork import QLocalServer
    from PySide6.QtCore import QTimer
    p=socket_path();p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock=p.with_suffix('.lock');fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:os.close(fd);return False
    window.control_lock=fd
    QLocalServer.removeServer(str(p));server=QLocalServer(window);server.setSocketOptions(QLocalServer.UserAccessOption)
    if not server.listen(str(p)):raise RuntimeError('control_unavailable')
    os.chmod(p,0o600);window.control_server=server;window.reload_requested=False
    def status():
        elapsed=max(0,__import__('time').monotonic()-window.started) if window.task else getattr(window,'finished_elapsed',0)
        import comparison
        return {'running':window.task is not None,'comparison_running':comparison.activity()['active'],'phase':window.phase.lower(),'outcome':window.outcome,'result_count':len(window.results),'elapsed_seconds':round(elapsed,2),'reload_pending':window.reload_requested}
    def accept():
        client=server.nextPendingConnection();buffer=bytearray()
        def receive():
            buffer.extend(bytes(client.readAll()))
            if len(buffer)>8192:client.abort();return
            if b'\n' not in buffer:return
            try:
                command=json.loads(bytes(buffer).split(b'\n')[0]);action=command.get('action')
                if action not in ('status','show','search','cancel','reload') or set(command)-{'action','query','scope','mode','refresh'}:raise ValueError()
                reply={'accepted':True}
                if action=='search':
                    if window.task:reply={'accepted':False,'error':'busy'}
                    else:
                        query=command.get('query')
                        if not isinstance(query,str) or not query.strip() or len(query)>4000:raise ValueError()
                        if 'scope' in command:
                            index=window.scope.findData(command['scope'])
                            if index<0:raise ValueError()
                            window.scope.setCurrentIndex(index)
                        if 'mode' in command:
                            if command['mode'] not in ('Hybrid','Keywords','Filenames','Deep'):raise ValueError()
                            window.mode.setCurrentText(command['mode'])
                        if 'refresh' in command:
                            if type(command['refresh']) is not bool:raise ValueError()
                            window.refresh.setChecked(command['refresh'])
                        window.query.setText(query);window.tabs.setCurrentIndex(0);window.start_search()
                elif action=='cancel':window.cancel_search()
                elif action=='reload':window.reload_requested=True;QTimer.singleShot(0,window.close)
                elif action=='show':window.show();window.raise_();window.activateWindow()
                reply.update(status())
            except Exception:reply={'accepted':False,'error':'invalid_command'}
            client.write(json.dumps(reply).encode()+b'\n');client.flush();client.disconnectFromServer()
        client.readyRead.connect(receive);client.disconnected.connect(client.deleteLater)
        QTimer.singleShot(0,receive)
    server.newConnection.connect(accept)
    return True

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description='Payload-free local GUI control; search text comes from stdin')
    p.add_argument('action',choices=['status','show','search','cancel','reload']);p.add_argument('--scope');p.add_argument('--mode',choices=['Hybrid','Keywords','Filenames','Deep']);args=p.parse_args()
    command={'action':args.action}
    if args.action=='search':command['query']=sys.stdin.read(4001)
    if args.scope is not None:command['scope']=args.scope
    if args.mode is not None:command['mode']=args.mode
    try:print(json.dumps(request(command)))
    except Exception:print(json.dumps({'accepted':False,'error':'unavailable'}));sys.exit(1)
