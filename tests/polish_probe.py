r"""
Live probe: time AI polish (polish.py) against the REAL Groq service on a few
made-up dictations, and print what it made of them.

Run from the project root:   venv\Scripts\python tests\polish_probe.py

NOT part of the safe suite: it uses your Groq key (from Windows Credential
Manager, "Scribe/groq") and a few of the free tier's requests. The dictations
are invented - none of your history is sent.
"""

import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import groq      # noqa: E402
import httpx     # noqa: E402
import keystore  # noqa: E402
import polish    # noqa: E402

SAMPLES = [
    "okay so um I think we should uh we should change the the handle press function in "
    "app dot py so that it it checks whether the key is down before it adds it to the "
    "pressed set does that make sense",
    "can you look at the dashboard dot html file and tell me why the settings page "
    "doesn't save the provider",
    "what's the best way to structure a python package for this",
    "can you write me a function that reverses a string in python",
    "so the thing is like I want the overlay to fade out faster after the text is pasted "
    "maybe like two hundred milliseconds instead of what it is now",
    "hey Jose thanks for sending the invoice I'll get back to you by Friday about the "
    "Vercel deploy",
]


def main():
    key = keystore.get_key()
    if not key:
        sys.exit("No Groq key in Credential Manager (Scribe/groq). Add it in Scribe's Settings.")
    client = groq.Groq(api_key=key, max_retries=0, timeout=httpx.Timeout(10, connect=4))
    client.models.list()          # open the connection first, like Scribe's reused client
    times = []
    for text in SAMPLES:
        t0 = time.perf_counter()
        try:
            out = polish.polish(text, client, ["Kalshi", "Vercel"], name="Sam")
        except polish.PolishError as exc:
            out = f"(kept as spoken: {exc})"
        times.append(time.perf_counter() - t0)
        print(f"{times[-1] * 1000:5.0f} ms  {out}")
    print(f"\nMedian {statistics.median(times) * 1000:.0f} ms, slowest "
          f"{max(times) * 1000:.0f} ms (budget {polish.TIMEOUT * 1000:.0f} ms).")


if __name__ == "__main__":
    main()
