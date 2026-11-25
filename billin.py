from flask import Flask, redirect, render_template, request, url_for
from livereload import Server

app = Flask(__name__)
app.debug = True

anggota = [{"nama": "Melissa", "pembuat": True}]

@app.route("/")
def index():
    return render_template('index.html')

@app.route("/tambah-anggota", methods=["GET", "POST"])
def tambah_anggota():
    if request.method == "POST":
        anggota.append({"nama": request.form["nama"], "pembuat": False})
        return redirect(url_for("tambah_anggota"))
    return render_template("tambah_anggota.html",
                           anggota=anggota,
                           invite_link=request.host_url + "join")

@app.route("/lanjutkan", methods=["POST"])
def lanjutkan():
    return redirect(url_for("index"))  # sesuaikan dengan halaman tujuan

if __name__ == "__main__":
    server = Server(app.wsgi_app)
    server.watch("templates/*")
    server.watch("static/css/*")
    server.serve(port=5000)