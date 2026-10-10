#!/usr/bin/env python3
"""Searchfu desktop client. Private results stay in local widgets and pipes."""
from pathlib import Path
import json, os, signal, sqlite3, subprocess, sys, threading, time
from contextlib import closing


def browse(db, text='', scope='', after='', limit=100, cancelled=None):
    from search import connect
    from collections_config import scope_sql
    where,params,_=scope_sql([scope] if scope else [])
    where+=['f.path>?'];params.append(after)
    if text:where.append('instr(lower(f.path),lower(?))>0');params.append(text)
    with closing(connect(Path(db),readonly=True)) as c:
        if cancelled:c.set_progress_handler(lambda:int(cancelled()),10000)
        return list(c.execute('SELECT f.path,f.kind,f.size,f.mtime FROM files f WHERE '+' AND '.join(where)+' ORDER BY f.path LIMIT ?',params+[limit]))


def worker():
    request=json.load(sys.stdin)
    from retrieval import retrieve
    stop=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    print(json.dumps({'stage':'opening','complete':False}),flush=True)
    print(json.dumps({'stage':'filename_scan' if request['options'].get('names_only') else 'keyword_scan','complete':False}),flush=True)
    for e in retrieve(request['db'],request['query'],cancelled=stop.is_set,**request['options']):
        print(json.dumps(e),flush=True)


def main(db=None):
    from PySide6.QtCore import QThread, Signal, Qt, QTimer, QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import (QApplication,QMainWindow,QWidget,QTabWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLineEdit,QPushButton,QComboBox,QCheckBox,QSpinBox,QLabel,QTableWidget,QTableWidgetItem,QTextEdit,QSplitter,QFileDialog,QMessageBox,QGroupBox)
    import comparison, collections_config
    from runtime_paths import activate
    db=Path(db or activate()['SEARCHFU_DB'])
    class Task(QThread):
        event=Signal(dict);failed=Signal();terminal=Signal()
        def __init__(self,request):super().__init__();self.request=request;self.process=None;self.stop=threading.Event()
        def run(self):
            try:
                env={**os.environ,'SEARCHFU_DB':str(db),'SEARCHFU_CUDA':'0'}
                self.process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,env=env)
                self.process.stdin.write(json.dumps(self.request));self.process.stdin.close()
                if self.stop.is_set():self.process.terminate()
                for line in self.process.stdout:
                    self.event.emit(json.loads(line))
                if self.process.wait()!=0 and not self.stop.is_set():self.failed.emit()
            except Exception:self.failed.emit()
            finally:
                if self.process and self.process.poll() is None:self.process.terminate();self.process.wait()
                self.terminal.emit()
        def cancel(self):
            self.stop.set()
            if self.process and self.process.poll() is None:self.process.terminate()
    class LocalTask(QThread):
        result=Signal(object);failed=Signal()
        def __init__(self,fn):super().__init__();self.fn=fn
        def run(self):
            try:self.result.emit(self.fn(lambda:self.isInterruptionRequested()))
            except Exception:self.failed.emit()
    class Window(QMainWindow):
        def __init__(self):
            super().__init__();self.setWindowTitle('Searchfu');self.resize(1120,780);self.task=None;self.started=0;self.phase='Ready';self.outcome=None;self.results=[];self.cursor='';self.browse_rows=[];self.local_tasks=[];self.browse_key=None
            self.tabs=QTabWidget();self.setCentralWidget(self.tabs)
            self.search_page();self.browse_page();self.metrics_page();self.settings_page()
            self.tabs.currentChanged.connect(self.tab_changed);self.reload_scopes();self.metrics_timer=QTimer(self);self.metrics_timer.timeout.connect(self.update_metrics);self.metrics_timer.start(5000);self.activity_timer=QTimer(self);self.activity_timer.timeout.connect(self.activity_tick);self.activity_timer.start(250);self.update_metrics()
        def page(self,title):
            page=QWidget();layout=QVBoxLayout(page);self.tabs.addTab(page,title);return layout
        def button(self,text,handler):
            b=QPushButton(text);b.clicked.connect(handler);return b
        def search_page(self):
            l=self.page('Search');l.addWidget(QLabel('Search your local index by keywords, meaning, or filename.'))
            row=QHBoxLayout();self.query=QLineEdit();self.query.setPlaceholderText('A few distinctive terms…');self.query.returnPressed.connect(self.start_search)
            self.go=self.button('Search',self.start_search);self.cancel_button=self.button('Cancel',self.cancel_search);self.cancel_button.setEnabled(False)
            row.addWidget(self.query,1);row.addWidget(self.go);row.addWidget(self.cancel_button);l.addLayout(row)
            row=QHBoxLayout();self.mode=QComboBox();self.mode.addItems(['Hybrid','Keywords','Filenames','Deep']);self.scope=QComboBox();self.top=QSpinBox();self.top.setRange(1,100);self.top.setValue(12);self.refresh=QCheckBox('Repair stale indexed files');self.refresh.setChecked(True)
            for w in (QLabel('Mode'),self.mode,QLabel('Folder / collection'),self.scope,QLabel('Results'),self.top,self.refresh):row.addWidget(w)
            l.addLayout(row);self.progress=QLabel('Ready. Queries and results stay on this computer.');l.addWidget(self.progress)
            split=QSplitter(Qt.Vertical);self.table=QTableWidget(0,3);self.table.setHorizontalHeaderLabels(['File','Match','Score']);self.table.setSelectionBehavior(QTableWidget.SelectRows);self.table.setEditTriggers(QTableWidget.NoEditTriggers);self.table.horizontalHeader().setStretchLastSection(True);self.table.setColumnWidth(0,720)
            self.table.itemSelectionChanged.connect(self.preview_result);self.table.cellDoubleClicked.connect(lambda *_:self.open_result())
            self.preview=QTextEdit();self.preview.setReadOnly(True);split.addWidget(self.table);split.addWidget(self.preview);l.addWidget(split,1);l.addWidget(self.button('Open selected file',self.open_result));l.addWidget(QLabel('Activity console • stages and counts only • not saved'));self.console=QTextEdit();self.console.setReadOnly(True);self.console.setMaximumHeight(130);self.console.document().setMaximumBlockCount(100);l.addWidget(self.console)
        def browse_page(self):
            l=self.page('Browse index');l.addWidget(QLabel('Browse stored filenames without walking your drives.'))
            row=QHBoxLayout();self.browse_filter=QLineEdit();self.browse_filter.setPlaceholderText('Filter stored paths…');self.browse_filter.returnPressed.connect(self.browse_reset);self.browse_scope=QComboBox();row.addWidget(self.browse_filter,1);row.addWidget(self.browse_scope);row.addWidget(self.button('Browse',self.browse_reset));l.addLayout(row)
            self.browse_table=QTableWidget(0,3);self.browse_table.setHorizontalHeaderLabels(['File','Type','Bytes']);self.browse_table.setColumnWidth(0,820);self.browse_table.setSelectionBehavior(QTableWidget.SelectRows);self.browse_table.setEditTriggers(QTableWidget.NoEditTriggers);self.browse_table.cellDoubleClicked.connect(lambda *_:self.open_browse());l.addWidget(self.browse_table,1)
            row=QHBoxLayout();row.addWidget(self.button('First page',self.browse_reset));self.next_button=self.button('Next 100',self.browse_next);row.addWidget(self.next_button);row.addWidget(self.button('Open selected file',self.open_browse));l.addLayout(row);self.browse_status=QLabel('Choose Browse to read the catalog.');l.addWidget(self.browse_status)
        def metrics_page(self):
            l=self.page('Comparison');l.addWidget(QLabel('Same search on NVMe and USB • USB is read-only • one shadow at a time'))
            self.comparison_status=QLabel('Checking comparison configuration…');self.comparison_status.setWordWrap(True);l.addWidget(self.comparison_status);self.metrics=QTextEdit();self.metrics.setReadOnly(True);l.addWidget(self.metrics,1)
            row=QHBoxLayout();row.addWidget(self.button('Refresh metrics',self.update_metrics));row.addWidget(self.button('Measure index footprint',self.measure_footprint));row.addWidget(self.button('Export safe metrics',self.export_metrics));row.addWidget(self.button('Reset metrics',self.reset_metrics));l.addLayout(row)
            self.footprint=QLabel('Footprint is measured on demand.');self.footprint.setWordWrap(True);l.addWidget(self.footprint)
            note=QLabel('Only aggregate timings, result counts and overlap are saved. No query history, filenames, snippets, hashes, IDs or per-search timestamps. Concurrent IO, cache warmth and differing index coverage affect timings; overlap measures agreement, not accuracy. Total primary latency includes freshness work. Index stages exclude query encoding/model startup (but include ranking); after repair, they describe the final pass. The USB index is never refreshed.');note.setWordWrap(True);l.addWidget(note)
        def settings_page(self):
            l=self.page('Settings')
            from runtime_paths import pinned_paths
            primary=pinned_paths();box=QGroupBox('Active primary index (NVMe)');form=QFormLayout(box)
            self.primary_paths={}
            for label,key in [('Database','SEARCHFU_DB'),('Matrix folder','SEARCHFU_ANN_DIR'),('Candidate folder','SEARCHFU_CANDIDATES_DIR')]:
                w=QLineEdit(str(db) if key=='SEARCHFU_DB' else primary[key]);w.setReadOnly(True);self.primary_paths[key]=w;form.addRow(label,w)
            l.addWidget(box);box=QGroupBox('Search comparison');form=QFormLayout(box)
            self.enabled=QCheckBox('Clone searches to USB');self.usb_db=QLineEdit();self.usb_ann=QLineEdit();self.usb_candidates=QLineEdit();self.timeout=QSpinBox();self.timeout.setRange(1,300)
            form.addRow(self.enabled)
            for label,w,directory in [('USB database',self.usb_db,False),('USB matrix folder',self.usb_ann,True),('USB candidate folder',self.usb_candidates,True)]:
                row=QHBoxLayout();row.addWidget(w);row.addWidget(self.button('Choose…',lambda _=False,w=w,d=directory:self.pick(w,d)));form.addRow(label,row)
            self.usb_db.setPlaceholderText('Not configured — choose the existing USB database');self.usb_db.textChanged.connect(self.usb_defaults);self.usb_ann.textChanged.connect(self.usb_defaults);self.usb_candidates.textChanged.connect(self.usb_defaults);self.usb_effective=QLabel();self.usb_effective.setWordWrap(True);form.addRow('Effective USB paths',self.usb_effective);form.addRow('Shadow timeout (seconds)',self.timeout);form.addRow(self.button('Save comparison settings',self.save_settings));l.addWidget(box)
            cfg=comparison.load();self.enabled.setChecked(cfg['enabled']);self.usb_db.setText(cfg['usb_db']);self.usb_ann.setText(cfg['usb_ann']);self.usb_candidates.setText(cfg['usb_candidates']);self.timeout.setValue(int(cfg['timeout_seconds']));self.usb_defaults()
            box=QGroupBox('Folder / collection views');form=QFormLayout(box);self.edit_scope=QComboBox();self.edit_scope.currentIndexChanged.connect(self.edit_collection);self.scope_name=QLineEdit();self.scope_root=QLineEdit();self.priority=QSpinBox();self.priority.setRange(-10000,10000);self.priority.setValue(100);form.addRow('Existing view',self.edit_scope);form.addRow('Name',self.scope_name)
            row=QHBoxLayout();row.addWidget(self.scope_root);row.addWidget(self.button('Choose…',lambda:self.pick(self.scope_root,True)));form.addRow('Folder',row);form.addRow('Priority (lower first)',self.priority);form.addRow(self.button('Save folder view',self.save_collection));form.addRow(self.button('Delete selected view',self.delete_collection));l.addWidget(box)
            note=QLabel('Views filter existing indexed paths. Adding a view does not index its folder or make missing content searchable. Index builds remain explicit CLI operations. NVMe remains the primary database.');note.setWordWrap(True);l.addWidget(note);l.addStretch()
        def usb_defaults(self):
            value=self.usb_db.text().strip()
            parent=Path(value).parent if value else None
            self.usb_ann.setPlaceholderText(str(parent/'ann') if parent else 'Uses ann beside the selected database')
            self.usb_candidates.setPlaceholderText(str(parent/'candidates') if parent else 'Uses candidates beside the selected database')
            self.usb_effective.setText(('Matrix: '+(self.usb_ann.text().strip() or str(parent/'ann'))+'\nCandidates: '+(self.usb_candidates.text().strip() or str(parent/'candidates'))) if parent else 'USB comparison is unavailable until an existing database is selected. Primary searches still use NVMe.')
        def activity_tick(self):
            if not self.task:return
            elapsed=time.monotonic()-self.started
            note=' • Waiting for indexed keyword ranking; common terms and cold disks can be slow.' if self.phase=='keyword_scan' and elapsed>=10 else ''
            self.progress.setText(f'{self.phase.replace("_"," ").capitalize()} • {elapsed:.1f}s elapsed • {len(self.results)} results'+note)
        def search_failed(self):
            self.outcome='failed';self.phase='failed';self.console.append('Search failed. No raw error output retained.');self.progress.setText('Search failed. Check index availability and settings.')
        def pick(self,w,directory):
            value=QFileDialog.getExistingDirectory(self,'Choose index folder') if directory else QFileDialog.getOpenFileName(self,'Choose existing USB database','','SQLite databases (*.sqlite3 *.sqlite *.db);;All files (*)')[0]
            if value:w.setText(value)
        def start_search(self):
            if self.task or not self.query.text().strip():return
            self.started=time.monotonic();self.phase='starting';self.outcome=None;self.console.clear();self.console.append('Starting local index search.');self.results=[];self.table.setRowCount(0);self.preview.clear();self.go.setEnabled(False);self.cancel_button.setEnabled(True)
            name=self.mode.currentText();options={'top_k':self.top.value(),'scopes':[self.scope.currentData()] if self.scope.currentData() else [],'fts_only':name=='Keywords','names_only':name=='Filenames','deep':name=='Deep','early':True,'refresh':self.refresh.isChecked(),'max_seconds':300}
            self.task=Task({'db':str(db),'query':self.query.text(),'options':options});self.task.event.connect(self.search_event);self.task.failed.connect(self.search_failed);self.task.finished.connect(self.search_finished);self.progress.setText('Starting…');self.task.start()
        def search_event(self,e):
            # Closed console vocabulary: never echo scope names, errors, queries or paths.
            stages={'opening','keyword_scan','filename_scan','keyword','encoding','candidate','candidates','semantic','quantized','full_precision','refine','refined','rerank','reranked','reranked_early','refresh','done','cancelled','deep'}
            self.phase=e['stage'] if e.get('stage') in stages else 'working'
            if self.phase in ('done','cancelled'):self.outcome=self.phase
            self.console.append(f'{time.monotonic()-self.started:.1f}s • {self.phase.replace("_"," ")} • {len(e.get("results",self.results))} results')
            self.progress.setText(f"{e['stage'].capitalize()} • {e.get('elapsed_seconds',0):.2f}s • {len(e.get('results',self.results))} results"+(f" • {e.get('coverage','')}" if e.get('coverage') else ''))
            if 'results' not in e:return
            self.results=e['results'];self.table.setRowCount(len(self.results))
            for i,r in enumerate(self.results):
                for j,value in enumerate((r['path'],r.get('match',''),str(r.get('score','')))):self.table.setItem(i,j,QTableWidgetItem(value))
        def search_finished(self):
            task=self.task;self.task=None;self.go.setEnabled(True);self.cancel_button.setEnabled(False)
            if task:task.deleteLater()
            if self.outcome is None:self.outcome='cancelled' if task and task.stop.is_set() else 'failed'
            self.progress.setText(f'{self.outcome.capitalize()} • {time.monotonic()-self.started:.1f}s elapsed • {len(self.results)} results')
            self.console.append('Worker exited • '+self.outcome)
            self.update_metrics()
        def cancel_search(self):
            if self.task:self.phase='cancelling';self.task.cancel();self.console.append('Cancellation requested; waiting for worker exit.');self.progress.setText('Cancelling… waiting for workers to exit.')
        def preview_result(self):
            i=self.table.currentRow()
            if 0<=i<len(self.results):self.preview.setPlainText(self.results[i].get('snippet') or 'Filename-only match; text is not indexed.')
        def open_path(self,path):QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        def open_result(self):
            i=self.table.currentRow()
            if 0<=i<len(self.results):self.open_path(self.results[i]['path'])
        def open_browse(self):
            i=self.browse_table.currentRow()
            if 0<=i<len(self.browse_rows):self.open_path(self.browse_rows[i][0])
        def browse_reset(self):self.cursor='';self.browse_next()
        def browse_next(self):
            key=(self.browse_filter.text(),self.browse_scope.currentData() or '')
            if key!=self.browse_key:self.cursor='';self.browse_key=key
            cursor=self.cursor;self.next_button.setEnabled(False);self.browse_status.setText('Reading stored catalog…')
            def show(rows):
                if key!=self.browse_key:return
                self.browse_rows=rows;self.browse_table.setRowCount(len(rows))
                for i,r in enumerate(rows):
                    for j,value in enumerate(r[:3]):self.browse_table.setItem(i,j,QTableWidgetItem(str(value)))
                if rows:self.cursor=rows[-1][0]
                self.next_button.setEnabled(len(rows)==100);self.browse_status.setText(f'{len(rows)} stored filenames on this page.')
            self.local_task(lambda stop:browse(db,key[0],key[1],cursor,cancelled=stop),show,lambda:self.browse_status.setText('Catalog unavailable. Check the primary index.'))
        def local_task(self,fn,show,failed):
            task=LocalTask(fn);self.local_tasks.append(task);task.result.connect(show);task.failed.connect(failed)
            def finished():
                self.local_tasks.remove(task);task.deleteLater()
            task.finished.connect(finished);task.start()
        def update_metrics(self):
            data=comparison.report();lines=[]
            for mode,v in data['modes'].items():
                n=v.get('paired',0);lines.append(f'{mode.capitalize()} — {int(n)} completed comparisons')
                if n:
                    for drive in ('nvme','usb'):
                        lines.append(f"  {drive.upper()}: mean {v.get(drive+'_seconds_sum',0)/n:.3f}s; first results {v.get(drive+'_first_seconds_sum',0)/n:.3f}s; index stages {v.get(drive+'_index_seconds_sum',0)/n:.3f}s; query encoding/model load {v.get(drive+'_encoding_seconds_sum',0)/n:.3f}s; mean results {v.get(drive+'_count_sum',0)/n:.1f}")
                    lines.append(f"  Mean result overlap: {100*v.get('overlap_sum',0)/n:.1f}% • USB faster: {int(v.get('usb_faster',0))}/{int(n)} • Same top result: {int(v.get('same_first',0))}/{int(n)}")
                skips=[f'{k.replace("_"," ")}: {int(v[k])}' for k in comparison.OUTCOMES if k!='paired' and v.get(k)]
                if skips:lines.append('  '+', '.join(skips))
                lines.append('')
            cfg=comparison.load()
            paired=sum(v.get('paired',0) for v in data['modes'].values())
            skipped=sum(v.get('missing_target',0) for v in data['modes'].values())
            if not cfg['enabled']:status='Comparison disabled. Enable cloning in Settings.'
            elif not cfg['usb_db']:status=f'Comparison not running: USB database not configured. {int(skipped)} searches skipped. Choose the existing USB database in Settings and save.'
            elif not Path(cfg['usb_db']).is_file():status='Comparison not running: configured USB database is unavailable. Check that the drive is mounted and the file exists.'
            else:status=f'Comparison enabled • {int(paired)} completed pairs. New searches launch a read-only USB shadow; timings appear after both searches finish.'
            self.comparison_status.setText(status)
            self.metrics.setPlainText('\n'.join(lines) or 'No completed comparisons yet. Search normally; this view updates automatically.')
        def measure_footprint(self):
            from runtime_paths import pinned_paths
            paths=pinned_paths();cfg=comparison.load();self.footprint.setText('Measuring index artifacts…')
            def run(stop):
                a=comparison.footprint(db,paths['SEARCHFU_ANN_DIR'],paths['SEARCHFU_CANDIDATES_DIR']);b=comparison.footprint(cfg['usb_db'],cfg['usb_ann'] or str(Path(cfg['usb_db']).parent/'ann'),cfg['usb_candidates'] or str(Path(cfg['usb_db']).parent/'candidates')) if cfg['usb_db'] else None
                return a,b
            def show(values):
                def fmt(v):return f"{sum(v[k] for k in ('database_bytes','journal_bytes','cache_bytes'))/1024**3:.2f} GiB logical / {sum(v[k] for k in ('database_allocated_bytes','journal_allocated_bytes','cache_allocated_bytes'))/1024**3:.2f} GiB allocated" if v and v['available'] else 'unavailable'
                self.footprint.setText(f'NVMe index + caches: {fmt(values[0])} • USB index + caches: {fmt(values[1])}')
            self.local_task(run,show,lambda:self.footprint.setText('Footprint measurement unavailable.'))
        def export_metrics(self):
            filename=QFileDialog.getSaveFileName(self,'Export aggregate metrics','searchfu-metrics.json','JSON (*.json)')[0]
            if filename:Path(filename).write_text(json.dumps(comparison.report(),indent=2)+'\n')
        def reset_metrics(self):
            if QMessageBox.question(self,'Reset metrics','Clear all aggregate comparison counters?')==QMessageBox.Yes:comparison.reset();self.update_metrics()
        def save_settings(self):
            try:
                comparison.save({'enabled':self.enabled.isChecked(),'usb_db':self.usb_db.text().strip(),'usb_ann':self.usb_ann.text().strip(),'usb_candidates':self.usb_candidates.text().strip(),'timeout_seconds':self.timeout.value()});self.update_metrics();QMessageBox.information(self,'Saved','Comparison settings saved. New searches use these settings.')
            except Exception:QMessageBox.warning(self,'Settings','Use absolute paths and a valid timeout.')
        def reload_scopes(self):
            data=collections_config.load()['collections']
            for combo in (self.scope,self.browse_scope):
                current=combo.currentData();combo.clear();combo.addItem('Entire index','')
                for name,e in sorted(data.items()):
                    if e.get('searchable',True):combo.addItem(name,name)
                if current:combo.setCurrentIndex(max(0,combo.findData(current)))
            self.edit_scope.blockSignals(True);self.edit_scope.clear();self.edit_scope.addItem('New view','')
            for name in sorted(data):self.edit_scope.addItem(name,name)
            self.edit_scope.blockSignals(False)
        def edit_collection(self):
            name=self.edit_scope.currentData() or '';entry=collections_config.load()['collections'].get(name,{})
            self.scope_name.setText(name);self.scope_root.setText(entry.get('root',''));self.priority.setValue(entry.get('priority',100))
        def save_collection(self):
            import re
            name=self.scope_name.text().strip();root=self.scope_root.text().strip()
            if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}',name) or not Path(root).is_absolute():QMessageBox.warning(self,'Folder view','Use a short lowercase name and an absolute folder path.');return
            data=collections_config.load();old=data['collections'].get(name,{})
            if old and old.get('root')!=root and old.get('one_filesystem'):QMessageBox.warning(self,'Folder view','This view pins a filesystem. Create a new view to use a different folder.');return
            data['collections'][name]={**old,'root':root,'priority':self.priority.value()};collections_config.save(data);self.reload_scopes()
        def delete_collection(self):
            name=self.edit_scope.currentData()
            if name and QMessageBox.question(self,'Delete view','Delete this folder view? Indexed files are retained.')==QMessageBox.Yes:
                data=collections_config.load();data['collections'].pop(name,None);collections_config.save(data);self.reload_scopes();self.edit_collection()
        def tab_changed(self,i):
            if i==2:self.update_metrics()
        def closeEvent(self,event):
            if self.task:self.cancel_search();event.ignore();self.task.finished.connect(self.close)
            elif self.local_tasks:
                for task in self.local_tasks:task.requestInterruption()
                event.ignore();QTimer.singleShot(100,self.close)
            else:event.accept()
    app=QApplication.instance() or QApplication(sys.argv);window=Window();window.show()
    if '--smoke-test' in sys.argv:
        # Tests supply synthetic DB/config; no real windows or content inspected.
        window.query.setText('synthetic');window.mode.setCurrentText('Keywords');window.refresh.setChecked(False);window.start_search()
        # Exercise a stalled first SQL stage without a real slow/private query.
        started=window.started;window.started-=12;window.phase='keyword_scan';window.activity_tick()
        if 'Waiting for indexed keyword ranking' not in window.progress.text():return 1
        window.started=started
        if not window.usb_effective.text() or not window.usb_ann.placeholderText():return 1
        saved=comparison.load();missing={**saved,'usb_db':''}
        with __import__('unittest.mock',fromlist=['patch']).patch('comparison.load',return_value=missing):window.update_metrics()
        if 'USB database not configured' not in window.comparison_status.text():return 1
        window.update_metrics()
        def verify():
            if window.task:return
            if not window.results or window.outcome!='done' or 'Worker exited' not in window.console.toPlainText() or window.primary_paths['SEARCHFU_DB'].text()!=str(db):app.exit(1);return
            if not window.browse_rows:
                if not window.local_tasks:window.browse_reset()
                return
            window.update_metrics();app.exit(0)
        timer=QTimer();timer.timeout.connect(verify);timer.start(50);QTimer.singleShot(20000,lambda:app.exit(2))
    return app.exec()

if __name__=='__main__':
    try:
        if '--worker' in sys.argv:worker()
        else:sys.exit(main())
    except Exception:
        print('Searchfu GUI operation failed.',file=sys.stderr);sys.exit(1)
