.PHONY: all build submodules clean test

all: build

submodules:
	git submodule update --init --recursive

build:
	@if [ ! -f vendor/modem/Makefile ]; then git submodule update --init --recursive; fi
	$(MAKE) -C vendor/modem encode decode CXX="g++ -march=native"
	$(MAKE) -C vendor/disorders CXX="g++ -march=native"

clean:
	$(MAKE) -C vendor/modem clean
	$(MAKE) -C vendor/disorders clean

test: build
	python3 tests/test_unit.py
	python3 tests/test_transport.py
	python3 tests/test_e2e.py
