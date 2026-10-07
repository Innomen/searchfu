"""Durable numeric run reports; private failure targets never leave SQLite."""
import errno,sqlite3

def initialize(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS indexing_runs(
        id INTEGER PRIMARY KEY,started REAL NOT NULL,finished REAL,
        names_only INTEGER NOT NULL,limited INTEGER NOT NULL,
        read_errors INTEGER NOT NULL DEFAULT 0,walk_errors INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS indexing_failures(
        run_id INTEGER NOT NULL REFERENCES indexing_runs(id),phase TEXT NOT NULL,
        category TEXT NOT NULL,target TEXT,errno INTEGER);
        CREATE INDEX IF NOT EXISTS indexing_failures_run ON indexing_failures(run_id,phase);''')

def category(exc):
    code=getattr(exc,'errno',None)
    if code in (errno.EACCES,errno.EPERM):return 'permission'
    if code in (errno.ENOENT,errno.ESTALE):return 'disappeared'
    if code in (errno.EIO,errno.ENODEV):return 'io'
    return 'other' if isinstance(exc,OSError) else 'unknown'

class WalkFailures(list):
    def __init__(self,c,run):super().__init__();self.c=c;self.run=run
    def record(self,exc=None,target=None):
        kind=category(exc);code=getattr(exc,'errno',None)
        target=target if target is not None else getattr(exc,'filename',None)
        with self.c:
            self.c.execute('INSERT INTO indexing_failures VALUES(?,?,?,?,?)',
                           (self.run,'walk',kind,str(target) if target is not None else None,code))
            self.c.execute('UPDATE indexing_runs SET walk_errors=walk_errors+1 WHERE id=?',(self.run,))
        super().append(kind)
    def append(self,value):self.record()  # Compatibility with custom walkers.

def record_walk(errors,exc=None,target=None):
    if errors is None:return
    if hasattr(errors,'record'):errors.record(exc,target)
    else:errors.append(True)

def summaries(c):
    try:
        rows=c.execute('SELECT id,started,finished,names_only,limited,read_errors,walk_errors FROM indexing_runs ORDER BY (walk_errors+read_errors>0 OR finished IS NULL) DESC,id DESC LIMIT 20').fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table: indexing_runs" in str(exc):return []  # Legacy index.
        raise
    result=[]
    for run,start,end,names,limited,reads,walks in rows:
        counts=dict(c.execute("SELECT category,count(*) FROM indexing_failures WHERE run_id=? AND phase='walk' GROUP BY category",(run,)))
        result.append({'run_id':run,'started':start,'finished':end,'filename_only':bool(names),'limited':bool(limited),
                       'read_errors':reads,'walk_errors':walks,'walk_error_categories':counts,
                       'coverage_complete':end is not None and not limited and not reads and not walks})
    return result
