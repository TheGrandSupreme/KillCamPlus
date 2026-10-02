# setup.py — build the native capture core into a local .pyd.
#
#   py -3.11 setup.py build_ext --inplace
#
# Requires: MSVC (VS 2022+), Windows SDK 10, pybind11 (`py -3.11 -m pip
# install pybind11`). Produces killcam_core.<tag>.pyd next to recorder.py.
# The app imports it opportunistically (use_native_core, default off);
# a missing/unbuilt .pyd never breaks the pure-Python path.
from setuptools import setup
from pybind11.setup_helpers import Pybind11Extension, build_ext


ext_modules = [
    Pybind11Extension(
        "killcam_core",
        ["killcam_core.cpp"],
        cxx_std=17,
        libraries=["d3d11", "dxgi"],
        extra_compile_args=["/EHsc", "/O2", "/W3"],
    ),
]

setup(
    name="killcam_core",
    version="1.0.0",
    description="KillCam+ native capture+feed core (DXGI -> ffmpeg pipes)",
    ext_modules=ext_modules,
    cmdclass={"build_ext": build_ext},
)
