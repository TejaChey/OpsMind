FROM postgres:16

# stress-ng is the fault-injection tool; it has to live inside the target container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends stress-ng \
    && rm -rf /var/lib/apt/lists/*
