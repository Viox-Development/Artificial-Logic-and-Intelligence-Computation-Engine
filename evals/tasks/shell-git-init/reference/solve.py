import subprocess


def git(*a):
    subprocess.run(["git", *a], check=True, capture_output=True)


git("init", "-q")
git("config", "user.name", "Eval Bot")
git("config", "user.email", "eval@example.com")
git("add", "-A")
git("commit", "-q", "-m", "Initial import")
git("tag", "v0.1.0")
