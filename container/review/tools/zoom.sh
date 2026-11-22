#!/bin/bash
# zoom.sh <pdf> <out.png> <x0 mm> <y0 mm> <w mm> <h mm> [dpi]: crop a region of a 1:1 board plot.
pdf=$1; out=$2; x0=$3; y0=$4; w=$5; h=$6; dpi=${7:-1200}
px() { python3 -c "print(int($1*$dpi/25.4))"; }
pdftoppm -r $dpi -png -singlefile -x $(px $x0) -y $(px $y0) -W $(px $w) -H $(px $h) "$pdf" "${out%.png}" 2>/dev/null
