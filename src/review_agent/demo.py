from .models import FileView, Snapshot


def sample_snapshot() -> Snapshot:
    return Snapshot(
        base_sha="demo-base", head_sha="demo-head",
        files=[FileView(
            path="app.py",
            content="def split_bill(total, count):\n    return total / count\n\n\n"
                    "def download(root, filename):\n    return (root / filename).read_text()\n",
            diff="@@ -0,0 +1,6 @@\n+def split_bill(total, count):\n+    return total / count\n+\n+\n"
                 "+def download(root, filename):\n+    return (root / filename).read_text()\n",
            changed_lines=[1, 2, 3, 4, 5, 6],
        )],
        limitations=["Synthetic sample with scripted outputs. No repository or model was accessed.",
                     "No tests executed; coverage is not measured."],
    )
