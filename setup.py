"""Sleep Classifier 项目的 setup.py。

纯粹为了兼容老工具链（还在看 ``setup.py`` 而不是 ``pyproject.toml``
的 CI / IDE）存在；真正的元数据来源是根目录下的 ``pyproject.toml``
（PEP 621 布局）。

版本号与 ``sleep_classifier/config.yaml`` 的 Add-on 版本保持
一致——改动时两处同步更新。
"""
from setuptools import find_packages, setup

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

with open("requirements-runtime.txt", "r", encoding="utf-8") as fh:
    requirements = [
        line.strip() for line in fh
        if line.strip() and not line.startswith("#")
    ]

setup(
    name="sleep-classifier",
    version="2.1.4",
    description=(
        "Home Assistant Add-on: learns your ideal sleep environment "
        "from your own HA sleep-stage history and adapts the bedroom "
        "across the night."
    ),
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/LiangyuLu-lly/HA-sleep",
    license="MIT",
    packages=find_packages(exclude=("tests", "tests.*")),
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Framework :: AsyncIO",
        "Topic :: Home Automation",
    ],
    python_requires=">=3.10",
    install_requires=requirements,
)
