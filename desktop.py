"""Install a desktop entry pointing to the same pinned Searchfu CLI."""
from pathlib import Path
import os

def install_desktop(launcher):
    directory=Path(os.environ.get('XDG_DATA_HOME',str(Path.home()/'.local/share')))/'applications'
    directory.mkdir(parents=True,exist_ok=True)
    # Desktop Exec quoting has its own grammar, not shell quoting.
    executable=str(Path(launcher)).replace('\\','\\\\').replace('"','\\"').replace('`','\\`').replace('$','\\$').replace('%','%%')
    target=directory/'searchfu.desktop'
    target.write_text('[Desktop Entry]\nType=Application\nName=Searchfu\nComment=Search, browse and compare your local indexes\nExec="'+executable+'" gui\nIcon=system-search\nTerminal=false\nCategories=Utility;Qt;\nKeywords=search;index;files;\n')
    return target
