#!/usr/bin/python

from distutils.core import setup
from distutils.extension import Extension
from distutils import sysconfig
from distutils.command.build_scripts import build_scripts
from Cython.Distutils import build_ext
import re
import os
import glob


# cannot import pypjua here
exec(file('pypjua/clients/setupconfig.py').read())

version = "0.3.0"

title = "SIP SIMPLE client"
description = "Python SIP SIMPLE client library using PJSIP"
scripts = [os.path.join('scripts', x) for x in os.listdir('scripts') if re.match('^sip_.*\\.py$', x) or re.match('^xcap_.*\\.py$', x)]
data_files = glob.glob(os.path.join('scripts', '*.wav'))

if data_files_dir:
    data_files = [(data_files_dir, data_files)]


def filter_cmdline(line, prefix):
    return [arg.split(prefix, 1)[1] for arg in line.split() if arg.startswith(prefix)]

build_mak_file = "pjsip/build.mak"
sysconfig._variable_rx = re.compile("(?:.*\s+)?([a-zA-Z][a-zA-Z0-9_]+)\s*:?=\s*(.*)")
build_mak = sysconfig.parse_makefile(build_mak_file)

includes = filter_cmdline(build_mak["APP_CFLAGS"], "-I")
lib_dirs = filter_cmdline(build_mak["APP_LDFLAGS"], "-L")
macros = [tuple(define.split("=", 1)) for define in filter_cmdline(build_mak["APP_CFLAGS"], "-D")]
libs = filter_cmdline(build_mak["APP_LDLIBS"], "-l")
extras = sum((build_mak["APP_LDLIBS"].split()[index:index+2] for index, value in enumerate(build_mak["APP_LDLIBS"].split()) if value == "-framework"), [])
depends = build_mak["APP_LIB_FILES"].split()

re_conditionals = re.compile(r"ifneq \((.*),(.*)\)\nAPP_THIRD_PARTY_LIBS\s\+=\s-l(.*?)-\$\(TARGET_NAME\)\nAPP_THIRD_PARTY_LIB_FILES\s\+=\s\$\(PJ_DIR\)(.*?)-\$\(LIB_SUFFIX\)")
re_findstring = re.compile(r"\$\(findstring\s(.*),(.*)\)")
for left, right, lib, lib_file in re_conditionals.findall(open(build_mak_file).read()):
    match = re_findstring.match(left)
    if match:
        to_find, find_in = match.groups()
        if to_find in find_in:
            left = "1"
        else:
            left = ""
    if left != right:
        libs.append("%s-%s" % (lib, build_mak["TARGET_NAME"]))
        depends.append("%s%s-%s" % (build_mak["PJ_DIR"], lib_file, build_mak["LIB_SUFFIX"]))

sysconfig._variable_rx = re.compile("([a-zA-Z][a-zA-Z0-9_]+)\s*=\s*(.*)")


if os.name == 'posix':
    class my_build_scripts(build_scripts):
        "remove .py extension from the scripts"

        def run (self):
            res = build_scripts.run(self)
            for script in self.scripts:
                filename = os.path.basename(script)
                if filename.endswith('.py'):
                    path = os.path.join(self.build_dir, filename)
                    print 'renaming %s -> %s' % (path, path[:-3])
                    os.rename(path, path[:-3])
else:
    my_build_scripts = build_scripts


setup(name         = "sipclient",
      version      = version,
      author       = "Ruud Klaver",
      author_email = "ruud@ag-projects.com",
      url          = "http://pypjua.com/",
      description  = title,
      long_description = description,
      license      = "GPL",
      platforms    = ["Platform Independent"],
      classifiers  = [
          #"Development Status :: 1 - Planning",
          #"Development Status :: 2 - Pre-Alpha",
          "Development Status :: 3 - Alpha",
          #"Development Status :: 4 - Beta",
          #"Development Status :: 5 - Production/Stable",
          #"Development Status :: 6 - Mature",
          #"Development Status :: 7 - Inactive",
          "Intended Audience :: Service Providers",
          "License :: GNU General Public License (GPL)",
          "Operating System :: OS Independent",
          "Programming Language :: Python",
          "Programming Language :: C"
      ],
      packages     = ["pypjua", "pypjua.clients", "pypjua.applications"],
      package_data = {
          'pypjua.applications' : ['xml-schemas/*']
      },
      data_files = data_files,
      scripts = scripts,
      ext_modules  = [
          Extension(name = "pypjua.core",
                    sources = ["pypjua/core.pyx", "pypjua/core.pxd"] + glob.glob(os.path.join("pypjua", "core.*.pxi")),
                    depends = depends,
		            include_dirs = includes,
                    library_dirs = lib_dirs,
                    define_macros = macros,
                    libraries = libs,
                    extra_compile_args = ["-Wno-unused-variable"],
                    extra_link_args = extras)
      ],
      cmdclass = { 'build_scripts' : my_build_scripts,
                   'build_ext': build_ext }
)
