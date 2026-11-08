"""Lazy offline fallback for the renderer's small passive SVG subset.

Uses the same layout/data as resvg. Gradients become flat translucent fills;
Matplotlib's Thai shaping can differ from resvg. Never loaded on the resvg path.
"""
from __future__ import annotations


def rasterize(svg, font):
    import io
    import re
    from plot_png import _validate_svg, PIXEL_RATIO
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.font_manager import FontProperties
    from matplotlib.patches import Circle, FancyBboxPatch, PathPatch
    from matplotlib.path import Path

    root = _validate_svg(svg)
    width, height = float(root.get('width')), float(root.get('height'))
    fig = Figure(figsize=(width/72, height/72), dpi=72*PIXEL_RATIO)
    canvas = FigureCanvasAgg(fig)
    ax = fig.add_axes((0,0,1,1)); ax.set_xlim(0,width); ax.set_ylim(height,0); ax.axis('off')
    prop = FontProperties(fname=str(font))
    for item in root:
        tag = item.tag.split('}')[-1]
        if tag == 'defs': continue
        a = item.attrib
        num = lambda k, default=0: float(a.get(k,default))
        fill, stroke = a.get('fill','none'), a.get('stroke','none')
        alpha = num('stroke-opacity',1)
        if fill == 'url(#usage-fill)': fill,alpha = '#53e7ff',0.09
        style = {'facecolor':fill,'edgecolor':stroke,'linewidth':num('stroke-width',1), 'alpha':alpha}
        if a.get('stroke-dasharray'): style['linestyle']=(0,tuple(map(float,a['stroke-dasharray'].split())))
        if tag == 'rect':
            rx=num('rx')
            ax.add_patch(FancyBboxPatch((num('x'),num('y')),num('width'),num('height'),
                                       boxstyle=f'round,pad=0,rounding_size={rx}',**style))
        elif tag == 'circle':
            ax.add_patch(Circle((num('cx'),num('cy')),num('r'),**style))
        elif tag == 'text':
            ax.text(num('x'),num('y'),item.text or '',fontproperties=prop,fontsize=num('font-size',13),
                    color=fill,ha={'start':'left','middle':'center','end':'right'}[a.get('text-anchor','start')],va='baseline')
        elif tag == 'line':
            kw = dict(color=stroke,linewidth=num('stroke-width',1),alpha=alpha,solid_capstyle='round')
            if 'linestyle' in style: kw['linestyle']=style['linestyle']
            ax.plot([num('x1'),num('x2')],[num('y1'),num('y2')],**kw)
        elif tag == 'path':
            tokens=re.findall(r'[MLZ]|-?\d+(?:\.\d+)?',a['d'])
            vertices,codes=[],[]
            i=0
            while i<len(tokens):
                command=tokens[i];i+=1
                if command=='Z': vertices.append(vertices[0]);codes.append(Path.CLOSEPOLY)
                else:
                    vertices.append((float(tokens[i]),float(tokens[i+1])));i+=2
                    codes.append(Path.MOVETO if command=='M' else Path.LINETO)
            ax.add_patch(PathPatch(Path(vertices,codes),**style))
    output=io.BytesIO()
    canvas.print_png(output)
    fig.clear()
    return output.getvalue()
