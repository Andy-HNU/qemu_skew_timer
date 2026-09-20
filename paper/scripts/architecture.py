#!/usr/bin/env python3
"""Draw design schematics from CSV labels; numeric positions are illustrative."""
import argparse
import csv
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from plot import style, save

def box(ax,x,y,label,w=2.8):
    ax.add_patch(FancyBboxPatch((x-w/2,y-.3),w,.6,boxstyle="round,pad=0.06",
                              facecolor="#E8F2F8",edgecolor="#0072B2"))
    ax.text(x,y,label,ha="center",va="center",fontsize=8)
def arrow(ax,a,b):
    ax.annotate("",xy=b,xytext=a,arrowprops=dict(arrowstyle="->",color="#555555"))
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv",required=True,type=Path)
    p.add_argument("--out",required=True,type=Path)
    a=p.parse_args()
    with a.csv.open(newline="",encoding="utf-8") as f:
        rows=list(csv.DictReader(f))
    labels={}
    for d in ("D1","D2","D3"):
        subset=sorted((r for r in rows if r["diagram"]==d),key=lambda r:int(r["order"]))
        labels[d]=[r["label"] for r in subset]
    if any(len(labels[d])!=n for d,n in (("D1",6),("D2",5),("D3",6))):
        raise ValueError("architecture CSV has missing or duplicate nodes")
    style()
    fig,ax=plt.subplots(figsize=(9,3.5))
    coords=[(1.5,2.5),(4.5,2.5),(7.5,2.5),(1.5,1),(4.5,1),(7.5,1)]
    for xy,label in zip(coords,labels["D1"]): box(ax,*xy,label)
    for start,end in ((0,1),(1,2),(4,5)):
        x,y=coords[start]; xx,yy=coords[end]
        arrow(ax,(x+1.45,y),(xx-1.45,yy))
    arrow(ax,(7.5,2.17),(4.5,1.35))
    arrow(ax,(7.5,2.17),(1.5,1.35))
    arrow(ax,(1.5,1.35),(1.5,2.17))
    ax.text(4.5,.25,"Budget plumbing is shared with icount; time-model state is separate.",
            ha="center",fontsize=8)
    ax.set(xlim=(-.2,9.2),ylim=(0,3));ax.axis("off")
    save(fig,a.out,"D1-architecture",False)
    fig,ax=plt.subplots(figsize=(7,3))
    ax.axvline(0,color="#555555",ls="--");ax.axvline(1,color="#D55E00",ls="--")
    for y,x,label in zip((3,2,1),(.2,.5,.85),labels["D2"][1:4]):
        ax.plot([0,x],[y,y],color="#0072B2",lw=3)
        ax.scatter(x,y,color="#0072B2")
        ax.text(x+.03,y,label,va="center")
    ax.text(0,3.8,labels["D2"][0],ha="center")
    ax.text(1,3.8,labels["D2"][4],ha="center")
    ax.annotate("",xy=(1,.4),xytext=(0,.4),arrowprops=dict(arrowstyle="<->"))
    ax.text(.5,.05,"W: allowed instruction lead (schematic)",ha="center")
    ax.set(xlim=(-.2,1.55),ylim=(-.25,4.2));ax.axis("off")
    save(fig,a.out,"D2-window",False)
    fig,ax=plt.subplots(figsize=(9,3.8))
    xy=[(1.5,2.5),(4.5,2.5),(7.5,2.5),(1.5,1),(4.5,1),(7.5,1)]
    for pos,label in zip(xy,labels["D3"]):box(ax,*pos,label)
    for start,end in ((0,1),(1,2),(3,4),(4,5)):
        x,y=xy[start];xx,yy=xy[end];arrow(ax,(x+1.45,y),(xx-1.45,yy))
    arrow(ax,(4.5,2.15),(4.5,1.35))
    arrow(ax,(7.5,2.15),(5.9,1.35))
    ax.text(4.5,.3,"Read: clamp to M +/- window, then monotonic publication; bound hits may plateau.",
            ha="center",fontsize=8)
    ax.set(xlim=(-.2,9.2),ylim=(0,3));ax.axis("off")
    save(fig,a.out,"D3-time-path",False)
if __name__=="__main__":main()
