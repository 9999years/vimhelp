FROM ubuntu
RUN apt update \
    && apt install -y python3 python3-pip git vim \
    && pip3 install 'Flask ~= 2.0'
COPY entrypoint.sh /opt/vimhelp/entrypoint.sh
COPY scripts/h2h.py /opt/vimhelp/scripts/h2h.py
COPY vimhelp/vimh2h.py /opt/vimhelp/vimhelp/vimh2h.py
ENTRYPOINT ["/opt/vimhelp/entrypoint.sh"]
