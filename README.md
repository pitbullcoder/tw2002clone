# tw2002clone
A Project Inspired by TradeWars 2002 BBS Game for MeshCore

# Setup
You will need to install Python3 in order to host the project. I also assume you have a MeshCore radio flashed with a USB Companion software. 
## One time setup steps for dependencies: <br/>
Open the python virtual environment: <br><br>
python3 -m venv venv<br>
source venv/bin/activate
<br><br>
Next install project dependencies: <br><br>
pip install meshcore
<br><br>
You will want to generate a galaxy prior to launching main.py. So start with this: 
<br><br>
python3 -m venv venv <br/>
source venv/bin/activate <br/>
python galaxy.py <br/>

# Startup
python3 -m venv venv <br/>
source venv/bin/activate <br/>
python main.py <br/>

# Testing
I started some unit tests. To run them: <br/><br/>
python3 -m venv venv <br/>
source venv/bin/activate <br/>
python main_test.py <br/>


