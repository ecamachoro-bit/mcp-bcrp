"""Gráfico de barras del porcentaje de aciertos por modo, a partir de resultados.csv.

    uv run grafico.py
"""
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

AQUI = Path(__file__).parent
ORDEN = ["memoria", "web", "mcp"]

res = pd.read_csv(AQUI / "resultados.csv")
aciertos = (res.resultado.eq("acierta").groupby(res.modo).mean() * 100).reindex(ORDEN).dropna()
n = res.groupby("modo").size()

fig, ax = plt.subplots(figsize=(6, 4), dpi=150, facecolor="#fcfcfb")
ax.set_facecolor("#fcfcfb")
barras = ax.bar(aciertos.index, aciertos.values, width=0.5, color="#2a78d6")
for b, (modo, pct) in zip(barras, aciertos.items()):
    ax.annotate(f"{pct:.0f} %", (b.get_x() + b.get_width() / 2, pct), xytext=(0, 4),
                textcoords="offset points", ha="center", va="bottom", color="#0b0b0b", fontsize=11)

ax.set_ylim(0, 110)
ax.set_yticks(range(0, 101, 25))
ax.set_yticklabels([f"{v} %" for v in range(0, 101, 25)])
ax.set_xticks(range(len(aciertos)))
ax.set_xticklabels([f"{m}\n(n = {n[m]})" for m in aciertos.index])
ax.set_title(f"Aciertos por modo · {res.id.nunique()} preguntas del BCRP", loc="left",
             color="#0b0b0b", fontsize=12)
ax.grid(axis="y", color="#e4e3df", linewidth=0.8)
ax.set_axisbelow(True)
for lado in ["top", "right", "left"]:
    ax.spines[lado].set_visible(False)
ax.spines["bottom"].set_color("#c3c2b7")
ax.tick_params(colors="#52514e", length=0)

fig.tight_layout()
fig.savefig(AQUI / "grafico.png")
print(aciertos.round(1).to_string())
