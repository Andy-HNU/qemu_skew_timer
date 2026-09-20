# Verified sources

Checked 2026-09-20. Source version pinned wherever available.

1. QEMU v10.2.0, TCG Instruction Counting:
   https://raw.githubusercontent.com/qemu/qemu/v10.2.0/docs/devel/tcg-icount.rst
   Local docs/devel/tcg-icount.rst. Establishes instruction accounting, lack of
   cycle timing, and incompatibility with MTTCG.
2. QEMU v10.2.0, Multi-threaded TCG:
   https://raw.githubusercontent.com/qemu/qemu/v10.2.0/docs/devel/multi-thread-tcg.rst
   Local docs/devel/multi-thread-tcg.rst. Establishes per-vCPU threads and
   single-thread fallback under icount.
3. Fabrice Bellard. QEMU, a Fast and Portable Dynamic Translator.
   USENIX Annual Technical Conference, FREENIX Track, 2005, pp. 41--46.
   https://www.usenix.org/legacy/event/usenix05/tech/freenix/full_papers/bellard/bellard.pdf
   Historical dynamic translation background only; not evidence of current MTTCG.
4. Project implementation snapshot:
   https://github.com/Andy-HNU/qemu_skew_timer/tree/4e894601b887007c180d5d28ec12afbba05c0965
   Current mechanism evidence, not independent validation.

No DOI has been invented. Official source / USENIX links are used instead.
The related-work survey is intentionally incomplete pending verified further sources.
