ARCH ?= $(shell uname -m)
MACOSX_DEPLOYMENT_TARGET ?= 14.0
export MACOSX_DEPLOYMENT_TARGET

.PHONY: all test package clean
all: build/brother-t236-print

build/brother-t236-print: src/print/brother-t236-print.c
	mkdir -p build
	xcrun clang -arch $(ARCH) -O2 -Wall -Wextra -Werror $< -o $@ -framework CoreGraphics -framework CoreFoundation -lcups
	codesign --force --sign - $@

test: all
	python3 -m unittest discover -s tests -v
	python3 scripts/manage.py install --dry-run --device-uri 'usb://Brother/DCP-T236?serial=EXAMPLE'

package: all
	python3 scripts/package.py --arch $(ARCH)

clean:
	rm -rf build dist
