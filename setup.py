#
# setup.py
#

from setuptools import setup, find_packages
import os
import glob

PACKAGE = 'atomsci'

here = os.path.abspath(os.path.dirname(__file__))
parent_dir = os.path.abspath(os.path.join(here, os.pardir))
script_files = glob.glob("scripts/*")

setup(
    name=f'{PACKAGE}-ampl',
    namespace_packages=[PACKAGE],
    include_package_data=True,
    version=open('VERSION').read().strip(),
    description=f'{PACKAGE} AMPL Python Package',
    zip_safe=False,
    data_files=[],
    packages=find_packages(),
    scripts=script_files,
    install_requires=[],
    entry_points={
        #        'console_scripts': [
        #            ("{pkg}_glo_command = "
        #             "{pkg}.glo:glo_command_main"
        #             .format(pkg=PACKAGE)),
        #        ],
    },
)
