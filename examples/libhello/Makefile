# Builds libhello.so.1 natively for whatever architecture the compiler targets.
# Inside an xarch-pkg chroot that is the chroot's architecture (via QEMU).

VERSION    = 1.0.0
SONAME     = libhello.so.1
CC        ?= gcc
CFLAGS    ?= -O2
LDFLAGS   ?=
PREFIX    ?= /usr
MULTIARCH ?= $(shell $(CC) -print-multiarch)
LIBDIR    ?= $(PREFIX)/lib/$(MULTIARCH)
INCDIR    ?= $(PREFIX)/include

all: $(SONAME)

hello.o: src/hello.c include/hello.h
	$(CC) $(CFLAGS) -fPIC -Wall -Wextra -Iinclude -DHELLO_VERSION='"$(VERSION)"' -c $< -o $@

$(SONAME): hello.o
	$(CC) $(LDFLAGS) -shared -Wl,-soname,$(SONAME) $^ -o $@

install: $(SONAME)
	install -d $(DESTDIR)$(LIBDIR) $(DESTDIR)$(INCDIR)
	install -m 0644 -s $(SONAME) $(DESTDIR)$(LIBDIR)/$(SONAME).0.0
	ln -sf $(SONAME).0.0 $(DESTDIR)$(LIBDIR)/$(SONAME)
	ln -sf $(SONAME) $(DESTDIR)$(LIBDIR)/libhello.so
	install -m 0644 include/hello.h $(DESTDIR)$(INCDIR)/hello.h

clean:
	rm -f hello.o $(SONAME)

.PHONY: all install clean
