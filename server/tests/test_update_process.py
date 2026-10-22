import os
import subprocess
import sys
import threading

import pytest

from app.core.update_process import ProbeTree, update_mutex


@pytest.mark.skipif(os.name != "nt", reason="Windows process ownership")
@pytest.mark.parametrize("accepted", [False, True])
def test_probe_tree_closure_only_preserves_accepted_process(accepted):
    tree = ProbeTree()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        tree.assign(proc)
        if accepted:
            tree.detach()
            assert proc.poll() is None
        else:
            tree.close()
            proc.wait(timeout=5)
            assert proc.poll() is not None
    finally:
        tree.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows update mutex")
def test_update_mutex_rejects_a_competing_thread():
    outcomes = []

    def competitor():
        try:
            with update_mutex():
                outcomes.append("entered")
        except OSError:
            outcomes.append("blocked")

    with update_mutex():
        thread = threading.Thread(target=competitor)
        thread.start()
        thread.join(timeout=3)
    assert outcomes == ["blocked"]
    with update_mutex():
        pass
