#!/bin/sh
set -eux
apk add --no-cache wget unzip gcompat libstdc++
cd /tmp
wget -q https://files.pythonhosted.org/packages/c5/9d/a42a84e10f1744dd27c6f2f9280cc3fb98f869dd19b7cd042e391ee2ab61/onnxruntime-1.20.1-cp312-cp312-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl
pip install --break-system-packages numpy coloredlogs flatbuffers protobuf sympy
unzip -q onnxruntime*.whl -d /usr/lib/python3.12/site-packages/
python3 -c "import onnxruntime; print('OK', onnxruntime.__version__)"
