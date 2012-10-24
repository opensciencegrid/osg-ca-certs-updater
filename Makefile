VERSION = 0.3
NAME = osg-ca-certs-updater
NAME_VERSION = $(NAME)-$(VERSION)
PYTHON_SITELIB = $(shell python -c "from distutils.sysconfig import get_python_lib; import sys; sys.stdout.write(get_python_lib())")
SBINDIR = /usr/sbin
SYSCONFDIR = /etc
INITRDDIR = $(SYSCONFDIR)/rc.d/init.d
CRONDDIR = $(SYSCONFDIR)/cron.d
DOCDIR = /usr/share/doc/$(NAME_VERSION)
MANDIR = /usr/share/man/man8
MANPAGE = $(NAME).8

MAN_DATE = $(shell date +'%B %d, %Y')

AFS_UPSTREAM_DIR = /p/vdt/public/html/upstream/$(NAME)

_default:
	@echo "Nothing to make. Try make install"

clean:
	rm -f *.py[co] *~

install: manual
	mkdir -p $(DESTDIR)/$(SBINDIR)
	install -p -m 755 $(NAME).py $(DESTDIR)/$(SBINDIR)/$(NAME)

	mkdir -p $(DESTDIR)/$(INITRDDIR)
	install -p -m 755 $(NAME)-cron.init $(DESTDIR)/$(INITRDDIR)/$(NAME)-cron

	mkdir -p $(DESTDIR)/$(CRONDDIR)
	install -p -m 644 $(NAME).cron $(DESTDIR)/$(CRONDDIR)/$(NAME)

	mkdir -p $(DESTDIR)/$(DOCDIR)
	install -p -m 644 README $(DESTDIR)/$(DOCDIR)
	
	mkdir -p $(DESTDIR)/$(MANDIR)
	install -p -m 644 $(MANPAGE) $(DESTDIR)/$(MANDIR)


dist:
	mkdir -p $(NAME_VERSION)
	cp -rp $(NAME).py $(NAME)-cron.init $(NAME).cron Makefile pylintrc $(MANPAGE).in README $(NAME_VERSION)/
	sed -i -e '/__version__/s/@VERSION@/$(VERSION)/' $(NAME_VERSION)/$(NAME).py
	tar czf $(NAME_VERSION).tar.gz $(NAME_VERSION)/ --exclude='*/.svn*' --exclude='*/*.py[co]' --exclude='*/*~'

afsdist upstream: dist
	mkdir -p $(AFS_UPSTREAM_DIR)/$(VERSION)
	mv -f $(NAME_VERSION).tar.gz $(AFS_UPSTREAM_DIR)/$(VERSION)/
	rm -rf $(NAME_VERSION)

manual: $(MANPAGE)

$(MANPAGE): $(MANPAGE).in
	sed -e 's/@NAME@/$(NAME)/g' -e 's/@DATE@/$(MAN_DATE)/g' -e 's/@VERSION@/$(VERSION)/g' $(MANPAGE).in > $(MANPAGE)
	

release: dist
	@if [ "$(DESTDIR)" = "" ]; then                                        \
		echo " ";                                                      \
		echo "ERROR: DESTDIR is required";                             \
		exit 1;                                                        \
	fi
	mkdir -p $(DESTDIR)/$(NAME)/$(VERSION)
	mv -f $(NAME_VERSION).tar.gz $(DESTDIR)/$(NAME)/$(VERSION)/
	rm -rf $(NAME_VERSION)

test:
	pylint -E --rcfile=pylintrc $(NAME).py

lint:
	-pylint --rcfile=pylintrc $(NAME).py
# ignore return code in above

# vim:ft=make:noet:ts=8:sts=8:sw=8

