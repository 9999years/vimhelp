FROM ubuntu
RUN apt update && apt install -y python3 python3-pip
RUN pip3 install 'Flask ~= 2.0'
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
