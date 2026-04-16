.PHONY: all build submodules clean

all: build

submodules:
	git submodule update --init --recursive

build:
	@if [ ! -f vendor/modem/Makefile ]; then git submodule update --init --recursive; fi
	$(MAKE) -C vendor/modem CXX="g++ -march=native"
	$(MAKE) -C vendor/disorders CXX="g++ -march=native"

clean:
	$(MAKE) -C vendor/modem clean
	$(MAKE) -C vendor/disorders clean
