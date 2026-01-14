export default function Home() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center p-8">
      <div className="text-center max-w-2xl">
        <h1 className="text-4xl font-bold mb-4">Linka Platform</h1>
        <p className="text-lg text-muted-foreground mb-8">Zambian SME E-Commerce Marketplace</p>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="p-6 border rounded-lg">
            <h2 className="font-semibold mb-2">Backend Services</h2>
            <p className="text-sm text-muted-foreground">
              Microservices architecture running on Docker with Supabase integration
            </p>
          </div>
          <div className="p-6 border rounded-lg">
            <h2 className="font-semibold mb-2">API Gateway</h2>
            <p className="text-sm text-muted-foreground">Centralized API gateway for all backend services</p>
          </div>
        </div>
      </div>
    </main>
  )
}
